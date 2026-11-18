/* Editor-neutral chat lifecycle. RPC authority remains in the native host. */
(function (root) {
  "use strict";
  const TERMINAL = new Set(["completed", "failed", "cancelled", "interrupted"]);
  const MODES = new Set(["readonly", "review", "auto"]);
  const text = (value, max = 100000) => typeof value === "string" ? value.slice(0, max) : "";
  class PortableChat {
    constructor(rpc, emit, nativeAction) {
      this.rpc = rpc;
      this.emit = emit;
      this.nativeAction = nativeAction;
      this.mode = "review";
      this.items = [];
      this.sessionId = null;
      this.jobId = null;
      this.status = "idle";
      this.connected = false;
      this.connecting = false;
      this.busy = false;
      this.generation = 0;
      this.sequence = 0;
      this.counter = 0;
      this.methods = new Set();
      this.accepted = null;
      this.error = "";
      this.model = "CLI configuration";
      this.requests = new Map();
      this.timer = null;
    }
    publish() {
      const ready = this.connected && !this.connecting;
      const blocker = {
        title: this.connecting ? "Connecting to Alysis Code" : "Connect your local agent",
        detail: this.error || "Select the Alysis CLI configured with your provider. This preview uses a development runtime.",
        progress: this.connecting,
        actions: this.connecting ? [] : [
          { label: "Choose CLI", command: "alysis.locateCli" },
          { label: "Connect", command: "alysis.showBridgeHealth" }
        ]
      };
      this.emit({ type: "state", state: {
        mode: this.mode, providerName: "Alysis Code", modelName: this.model,
        ready, readyReason: blocker.detail, readiness: { ok: ready, blockers: ready ? [] : [blocker] },
        engine: { label: "Local agent", detail: ready ? "Connected · development runtime" : blocker.detail, tone: ready ? "ready" : "attention" },
        workspace: { detail: "Project access is checked by the IDE before the agent starts." },
        provider: { detail: "Uses your existing CLI provider configuration." },
        conversation: { sessionId: this.sessionId, mode: this.mode, jobStatus: this.status, running: this.busy, items: this.items },
        recentTasks: [], commands: [
          { command: "alysis.locateCli", title: "Choose CLI", description: "Select your configured local Alysis executable", available: !this.busy },
          { command: "alysis.showBridgeHealth", title: "Connect", description: "Connect to the local agent", available: !this.busy },
          { command: "alysis.newSession", title: "New task", available: !this.busy },
          { command: "alysis.cancelCurrentRun", title: "Stop", available: this.busy }
        ], slashCommands: [], actionResults: [], runtimeEvents: [],
        lastAcceptedTaskRequestId: this.accepted
      } });
    }
    add(kind, content, extra = {}) {
      const item = { id: `portable-${++this.counter}`, kind, text: text(content), status: "complete", ...extra };
      this.items.push(item);
      // Never evict an unresolved approval. Stop rendering old completed items first.
      while (this.items.length > 200) {
        const index = this.items.findIndex(entry => entry.status !== "pending");
        if (index < 0) break;
        this.items.splice(index, 1);
      }
      return item;
    }
    async connect() {
      if (this.busy || this.connecting || this.connected) return;
      const generation = this.generation;
      this.connecting = true;
      this.error = "";
      this.publish();
      try {
        const health = await this.rpc("initialize", {});
        if (generation !== this.generation) return;
        this.methods = new Set(health.capabilities?.methods || []);
        if (health.protocol_version !== "1" || !["session.create", "chat.send", "session.cancel", "approval.respond", "job.status"].every(m => this.methods.has(m))) {
          throw new Error("This CLI does not support the required Alysis chat protocol. Update the CLI and reconnect.");
        }
        this.connected = true;
      } catch (error) {
        if (generation !== this.generation) return;
        this.connected = false;
        this.error = error.message || "Could not connect to the local agent.";
      } finally {
        if (generation === this.generation) {
          this.connecting = false;
          this.publish();
        }
      }
    }
    async submit(message) {
      if (typeof message.requestId !== "string" || !message.requestId || message.requestId.length > 128) return;
      const instruction = text(message.instruction, 20000).trim();
      const key = message.requestId;
      if (this.requests.has(key)) {
        const previous = this.requests.get(key);
        if (previous.instruction !== instruction) {
          this.emit({ type: "task.result", requestId: key, started: false });
          return;
        }
        const generation = this.generation;
        const started = await previous.promise;
        if (generation === this.generation) this.emit({ type: "task.result", requestId: key, started });
        return;
      }
      const promise = this.submitOnce(message, instruction);
      this.requests.set(key, { instruction, promise });
      if (this.requests.size > 128) this.requests.delete(this.requests.keys().next().value);
      await promise;
    }
    async submitOnce(message, instruction) {
      let accepted = false;
      let userItem;
      let started = false;
      const generation = this.generation;
      try {
        if (!instruction) throw new Error("Enter a task first.");
        if (!this.connected) throw new Error("Connect to the local agent first.");
        if (this.busy) throw new Error("Wait for this task to finish or stop it before sending another message.");
        if (instruction.startsWith("/")) throw new Error("Slash commands are not available in the JetBrains preview. Use the chat controls.");
        if (!MODES.has(message.mode)) throw new Error("Unsupported permissions.");
        this.busy = true;
        started = true;
        this.status = "starting";
        this.publish();
        if (!this.sessionId) {
          const created = await this.rpc("session.create", { mode: message.mode });
          if (generation !== this.generation) return;
          if (!created.session_id) throw new Error("The agent did not create a conversation.");
          this.sessionId = created.session_id;
          this.mode = created.mode || message.mode;
          this.sequence = 0;
          if (this.methods.has("session.modelInfo")) {
            const info = await this.rpc("session.modelInfo", { session_id: this.sessionId });
            if (generation !== this.generation) return;
            this.model = info.model || info.model_name || this.model;
          }
        } else if (message.mode !== this.mode) {
          if (!this.methods.has("session.setMode")) throw new Error("Update the CLI to change permissions.");
          const changed = await this.rpc("session.setMode", { session_id: this.sessionId, mode: message.mode, workspace_trusted: true });
          if (generation !== this.generation) return;
          this.mode = changed.mode || this.mode;
          if (this.mode !== message.mode) throw new Error("The agent did not accept the selected permissions.");
        }
        userItem = this.add("user", instruction);
        const job = await this.rpc("chat.send", { session_id: this.sessionId, message: instruction, idempotency_key: message.requestId });
        if (generation !== this.generation) return;
        if (!job.job_id) throw new Error("The agent did not start a task.");
        this.jobId = job.job_id;
        this.accepted = message.requestId;
        accepted = true;
        this.status = job.status || "running";
        if (TERMINAL.has(this.status)) this.finish(this.status);
        else this.schedulePoll();
      } catch (error) {
        if (generation !== this.generation) return;
        if (userItem) this.items = this.items.filter(item => item !== userItem);
        if (started) { this.busy = false; this.status = "failed"; }
        this.add("error", error.message || "The task could not start.");
      } finally {
        if (generation === this.generation) {
          this.emit({ type: "task.result", requestId: message.requestId, started: accepted });
          this.publish();
        }
      }
      return accepted;
    }
    schedulePoll() {
      clearTimeout(this.timer);
      const generation = this.generation;
      this.timer = setTimeout(async () => {
        if (!this.jobId || !this.busy || generation !== this.generation) return;
        try {
          const result = await this.rpc("job.status", { job_id: this.jobId });
          if (generation !== this.generation) return;
          this.status = result.status || this.status;
          if (TERMINAL.has(this.status)) this.finish(this.status);
          this.publish();
          if (this.busy) this.schedulePoll();
        } catch (error) {
          if (generation !== this.generation) return;
          this.disconnected(error.message || "The local agent stopped responding.");
        }
      }, 400);
      this.timer.unref?.();
    }
    finish(status) {
      this.status = status;
      this.busy = false;
      this.jobId = null;
      clearTimeout(this.timer);
      for (const item of this.items) {
        if (item.kind === "assistant" && item.status === "streaming") item.status = "complete";
        if (item.status === "pending" && item.kind === "approval") item.status = "expired";
      }
      if (status === "cancelled") this.add("notice", "Stopped.");
    }
    onEvent(event) {
      if (!this.sessionId || event.session_id !== this.sessionId || !this.busy) return;
      if (this.jobId && event.job_id && event.job_id !== this.jobId) return;
      if (!Number.isSafeInteger(event.sequence) || event.sequence <= this.sequence) return;
      this.sequence = event.sequence;
      const payload = event.payload || {};
      if (payload.worker_id || payload.role) return; // Child-worker streams are not the main reply.
      if (event.type === "message_delta" || event.type === "message_end") {
        let item = this.items[this.items.length - 1];
        if (!item || item.kind !== "assistant" || item.status !== "streaming") {
          if (event.type === "message_end" && item?.kind === "assistant" && item.text === payload.text) return;
          item = this.add("assistant", "", { status: "streaming" });
        }
        const next = event.type === "message_end" && typeof payload.text === "string" ? payload.text : item.text + text(payload.text);
        item.text = next.length > 100000 ? next.slice(0, 99950) + "\n\n[Reply preview truncated]" : next;
        if (event.type === "message_end") item.status = "complete";
      } else if (event.type === "prompt_for_input" && payload.kind === "approval") {
        if (this.items.some(item => item.approvalId === payload.approval_id)) return;
        this.add("approval", text(payload.reason || payload.prompt_text), {
          status: "pending", approvalId: payload.approval_id,
          allowForSession: payload.allow_for_session_supported === true,
          approval: { kind: text(payload.approval_kind), reason: text(payload.reason), preview: text(payload.preview),
            command: payload.command || null, files: Array.isArray(payload.files) ? payload.files : [],
            expiresAt: payload.expires_at || null, scope: "", warning: payload.allow_for_session_warning || null }
        });
      } else if (event.type === "error_raised") {
        this.add("error", text(payload.message || payload.error, 4000) || "The agent reported an error.");
      } else if (event.type === "tool_call_started") {
        this.add("notice", `Using ${text(payload.tool_name || payload.name, 100) || "a tool"}…`);
      }
      this.publish();
    }
    async approve(message) {
      const generation = this.generation;
      const item = this.items.find(entry => entry.approvalId === message.approvalId && entry.status === "pending");
      if (!item || !this.sessionId || !this.busy) return;
      if (!["allow_once", "allow_for_session", "deny"].includes(message.decision)) return;
      if (message.decision === "allow_for_session" && !item.allowForSession) return;
      const result = await this.rpc("approval.respond", {
        session_id: this.sessionId, approval_id: message.approvalId,
        allow: message.decision !== "deny", allow_for_session: message.decision === "allow_for_session"
      });
      if (generation !== this.generation) return;
      item.status = result.status === "expired" ? "expired" : message.decision;
      this.publish();
    }
    async cancel() {
      if (!this.busy || !this.sessionId) return;
      const generation = this.generation;
      const result = await this.rpc("session.cancel", { session_id: this.sessionId });
      if (generation !== this.generation) return;
      this.status = result.status || "cancellation_requested";
      if (this.status === "closed") { this.sessionId = null; this.finish("cancelled"); }
      else if (TERMINAL.has(this.status)) this.finish(this.status);
      this.publish();
    }
    async newSession() {
      if (this.busy) throw new Error("Stop the current task before starting a new one.");
      const generation = this.generation;
      this.busy = true;
      this.publish();
      try {
        if (this.sessionId) await this.rpc("session.cancel", { session_id: this.sessionId });
      } catch (error) {
        if (generation === this.generation) this.busy = false;
        throw error;
      }
      if (generation !== this.generation) return;
      this.generation++;
      this.busy = false;
      this.sessionId = null;
      this.jobId = null;
      this.items = [];
      this.requests.clear();
      this.accepted = null;
      this.status = "idle";
      this.publish();
    }
    disconnected(reason) {
      this.generation++;
      clearTimeout(this.timer);
      this.connected = false;
      this.connecting = false;
      this.requests.clear();
      this.accepted = null;
      this.error = reason;
      this.finish("interrupted");
      this.sessionId = null;
      this.publish();
    }
    async handle(message) {
      const generation = this.generation;
      try {
        if (!message || typeof message !== "object") return;
        if (message.type === "ready") this.publish();
        else if (message.type === "task.submit") await this.submit(message);
        else if (message.type === "task.cancel") await this.cancel();
        else if (message.type === "approval") await this.approve(message);
        else if (message.type === "cockpit" && message.message?.type === "mode.set") {
          const mode = message.message.mode;
          if (!MODES.has(mode) || this.busy) return;
          if (this.sessionId) {
            if (!this.methods.has("session.setMode")) throw new Error("Update the CLI to change permissions.");
            const result = await this.rpc("session.setMode", { session_id: this.sessionId, mode, workspace_trusted: true });
            if (generation !== this.generation) return;
            this.mode = result.mode || this.mode;
          } else this.mode = mode;
          this.publish();
        } else if (message.type === "action" && message.action === "new" || message.type === "command" && message.command === "alysis.newSession") await this.newSession();
        else if (message.type === "command" && message.command === "alysis.cancelCurrentRun") await this.cancel();
        else if (message.type === "command" && message.command === "alysis.showBridgeHealth") await this.connect();
        else if (message.type === "command" && message.command === "alysis.locateCli") {
          if (this.busy) return;
          if (await this.nativeAction({ type: "configure" })) { this.disconnected(""); await this.connect(); }
        } else if (["clipboard.copy", "open.external", "open.file"].includes(message.type)) await this.nativeAction(message);
        else throw new Error("This action is not available in the JetBrains preview yet.");
      } catch (error) {
        if (generation !== this.generation) return;
        this.add("error", error.message || "The action could not complete.");
        this.publish();
      }
    }
    dispose() { this.generation++; clearTimeout(this.timer); }
  }
  if (typeof module !== "undefined") module.exports = { PortableChat };
  else root.AlysisPortableChat = PortableChat;
})(typeof window !== "undefined" ? window : globalThis);
