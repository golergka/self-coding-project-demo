/* All displayed backend content is inserted as text, never HTML. */
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    project: "",
    session: "",
    mainCommit: "",
    generation: 0,
    busy: new Set(),
    timeline: "",
    projectName: "",
    runs: [],
    versions: [],
    selectedCommit: "",
    selectedFile: "",
    versionGeneration: 0,
    chatEntries: [],
    chatProgress: null,
    pendingChat: null,
    chatChecked: false,
    chatError: "",
  };
  const path = () => `/api/projects/${encodeURIComponent(state.project)}`;
  async function request(url, body) {
    const generation = state.generation;
    const options =
      body === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          };
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (url !== "/api/service" && generation !== state.generation)
      throw new Error("Project changed.");
    if (!response.ok)
      throw new Error(
        typeof data.detail === "string"
          ? data.detail
          : data.error || `Request failed (${response.status})`,
      );
    return data;
  }
  function status(id, text, error = false) {
    const el = $(id);
    el.textContent = text;
    el.classList.toggle("error", error);
  }
  function node(tag, text, parent) {
    const el = document.createElement(tag);
    el.textContent = text;
    parent.appendChild(el);
    return el;
  }
  function jsonObject(value) {
    const result = JSON.parse(value);
    if (!result || typeof result !== "object" || Array.isArray(result))
      throw new Error("Enter a JSON object.");
    return result;
  }
  async function action(button, statusId, fn) {
    const generation = state.generation;
    button.disabled = true;
    status(statusId, "Working…");
    try {
      await fn();
    } catch (e) {
      if (generation === state.generation) status(statusId, e.message, true);
    } finally {
      button.disabled = false;
    }
  }
  async function projects(selected = state.project) {
    const data = await request("/api/projects");
    const select = $("projects");
    select.replaceChildren(new Option("Select a project", ""));
    data.forEach((p) => select.add(new Option(p.name, p.id)));
    select.value = data.some((p) => p.id === selected)
      ? selected
      : data[0]?.id || "";
    return select.value;
  }
  function selectProject(id) {
    state.project = id;
    state.session = "";
    state.projectName = "";
    state.runs = [];
    state.versions = [];
    state.selectedCommit = "";
    state.selectedFile = "";
    state.versionGeneration++;
    for (const id of ["version-files", "version-file-content", "version-diff"])
      $(id).replaceChildren();
    $("selected-version").textContent = "Select a version";
    $("version-file-name").textContent = "Select a file";
    status("version-browser-status", "");
    state.chatEntries = [];
    state.chatProgress = null;
    state.pendingChat = null;
    state.chatChecked = false;
    state.chatError = "";
    $("chat-form").querySelector("button").disabled = true;
    $("run-status-filter").value = "all";
    $("run-scope-filter").value = "all";
    state.mainCommit = "";
    state.timeline = "";
    state.generation++;
    state.busy.clear();
    try {
      localStorage.setItem("self-coding-project", id);
    } catch (_) {}
    showView("dashboard");
    for (const field of [
      "dashboard-project-name",
      "dashboard-commit",
      "metric-runs",
      "metric-success",
      "metric-failed",
      "metric-version",
      "main-commit",
      "branches",
    ])
      $(field).textContent = "—";
    $("nav-run-count").textContent = "0";
    for (const field of [
      "recent-runs",
      "version-history",
      "timeline",
      "chat-history",
      "runs",
    ]) {
      $(field).replaceChildren();
    }
    $("main-target").value = "";
    for (const form of ["chat-form", "start-test", "event-form", "social-form"])
      $(form).reset();
    $("workspace").hidden = !id;
    $("empty-workspace").hidden = !!id;
    $("session").hidden = true;
    delete $("chat-history").dataset.signature;
    delete $("version-history").dataset.signature;
    $("social-output").textContent = "";
    delete $("runs").dataset.signature;
    for (const id of [
      "project-action",
      "chat-status",
      "test-status",
      "event-status",
      "social-status",
      "runs-status",
      "chat-refresh",
      "test-refresh",
      "social-refresh",
    ])
      status(id, "");
    $("start-test").elements.commit.value = "";
    if (id) refresh();
  }
  async function poll(key, statusId, get, render, valid = () => true) {
    if (!state.project || state.busy.has(key)) return;
    const generation = state.generation;
    state.busy.add(key);
    try {
      const data = await get();
      if (generation === state.generation && valid()) {
        render(data);
        status(statusId, "Updated");
      }
    } catch (e) {
      if (generation === state.generation && valid())
        status(statusId, e.message, true);
    } finally {
      if (generation === state.generation) state.busy.delete(key);
    }
  }
  function refresh() {
    poll(
      "project",
      "project-status",
      () => request(path()),
      (p) => {
        $("main-commit").textContent = p.main_commit || "—";
        state.projectName = p.name;
        $("page-description").textContent = p.name;
        $("dashboard-project-name").textContent = p.name;
        $("dashboard-commit").textContent = p.main_commit
          ? p.main_commit.slice(0, 7)
          : "No version yet";
        $("metric-version").textContent = p.main_commit
          ? p.main_commit.slice(0, 7)
          : "—";
        $("branches").textContent = Object.entries(p.branches)
          .map(([branch, commit]) => branch + "  " + commit)
          .join("\n");
        const input = $("start-test").elements.commit;
        if (!input.value || input.value === state.mainCommit)
          input.value = p.main_commit || "";
        const target = $("main-target");
        if (!target.value || target.value === state.mainCommit)
          target.value = p.main_commit || "";
        state.mainCommit = p.main_commit || "";
      },
    );
    poll(
      "history",
      "project-status",
      () => request(path() + "/history"),
      (history) => {
        state.versions = history;
        if (!state.selectedCommit && history.length)
          selectVersion(history[0].commit);
        renderVersions();
      },
    );
    poll(
      "chat",
      "chat-refresh",
      () => request(path() + "/chat"),
      (entries) => {
        state.chatEntries = entries;
        renderChat();
      },
    );
    poll(
      "chat-state",
      "chat-refresh",
      () => request(path() + "/chat/status"),
      (progress) => {
        state.chatProgress = progress;
        state.chatChecked = true;
        renderChat();
      },
    );
    poll(
      "runs",
      "runs-status",
      () => request(path() + "/runs"),
      (runs) => {
        $("metric-runs").textContent = runs.length;
        const completed = runs.filter(
          (run) => run.status === "completed",
        ).length;
        const finished = runs.filter((run) =>
          ["completed", "failed", "uncertain"].includes(run.status),
        ).length;
        $("metric-success").textContent = finished
          ? Math.round((completed * 100) / finished) + "%"
          : "—";
        $("nav-run-count").textContent = runs.length;
        $("metric-failed").textContent = runs.filter(
          (run) => run.status === "failed" || run.status === "uncertain",
        ).length;
        const recent = $("recent-runs");
        recent.replaceChildren();
        if (!runs.length) node("p", "No runs yet.", recent);
        runs
          .slice(-5)
          .reverse()
          .forEach((run) => {
            const row = node("button", "", recent);
            row.type = "button";
            row.className = "recent-row";
            row.addEventListener("click", () => {
              $("run-status-filter").value = "all";
              $("run-scope-filter").value = "all";
              showView("runs");
              renderRuns(run.id);
              document
                .getElementById("run-" + run.id)
                ?.scrollIntoView({ block: "nearest" });
            });
            const identity = node("div", "", row);
            node("strong", run.id.slice(0, 8), identity);
            node(
              "small",
              run.workflow_id
                ? `Workflow ${run.workflow_id.slice(0, 8)}`
                : "No workflow",
              identity,
            );
            node("span", run.status, row);
            node("span", run.test_session_id ? "Test" : "Live", row);
          });
        state.runs = runs;
        renderRuns();
      },
    );
    if (state.session) {
      const session = state.session;
      poll(
        "session",
        "test-refresh",
        () => request(`/api/tests/${encodeURIComponent(session)}`),
        renderSession,
        () => state.session === session,
      );
    }
    poll(
      "social",
      "social-refresh",
      () => request(path() + "/debug/social"),
      (data) => {
        $("social-output").textContent = JSON.stringify(data, null, 2);
      },
    );
  }
  function renderVersions() {
    const box = $("version-history");
    const signature = JSON.stringify([state.versions, state.selectedCommit]);
    if (box.dataset.signature === signature) return;
    box.dataset.signature = signature;
    box.replaceChildren();
    if (!state.versions.length) node("p", "No versions on main yet.", box);
    state.versions.forEach((version) => {
      const button = node("button", "", box);
      button.type = "button";
      button.className =
        "version-history-item" +
        (version.commit === state.selectedCommit ? " active" : "");
      button.setAttribute(
        "aria-pressed",
        String(version.commit === state.selectedCommit),
      );
      node("strong", version.message, button);
      node(
        "small",
        `${version.commit.slice(0, 7)} · ${new Date(version.timestamp * 1000).toLocaleString()}`,
        button,
      );
      button.addEventListener("click", () => selectVersion(version.commit));
    });
  }
  async function selectVersion(commit) {
    state.selectedCommit = commit;
    state.selectedFile = "";
    const selection = ++state.versionGeneration;
    renderVersions();
    $("selected-version").textContent = `Commit ${commit.slice(0, 7)}`;
    $("version-file-name").textContent = "Select a file";
    $("version-file-content").textContent = "";
    $("version-files").replaceChildren();
    $("version-diff").textContent = "";
    status("version-browser-status", "Loading…");
    const base = path() + "/commits/" + encodeURIComponent(commit);
    try {
      const [tree, diff] = await Promise.all([
        request(base + "/files"),
        request(base + "/diff"),
      ]);
      if (selection !== state.versionGeneration) return;
      if (!tree.files.length)
        node("p", "No files in this version.", $("version-files"));
      tree.files.forEach((file) => {
        const button = node("button", file.path, $("version-files"));
        button.type = "button";
        button.className = "version-file-item";
        button.addEventListener("click", () =>
          selectVersionFile(commit, file.path, button),
        );
      });
      $("version-diff").textContent =
        (diff.diff || "No changes.") +
        (diff.truncated ? "\n\n[Diff truncated]" : "");
      status("version-browser-status", "");
    } catch (error) {
      if (selection === state.versionGeneration)
        status("version-browser-status", error.message, true);
    }
  }
  async function selectVersionFile(commit, file, button) {
    state.selectedFile = file;
    const selection = state.versionGeneration;
    document
      .querySelectorAll(".version-file-item")
      .forEach((item) => item.classList.toggle("active", item === button));
    $("version-file-name").textContent = file;
    $("version-file-content").textContent = "Loading…";
    try {
      const result = await request(
        path() +
          "/commits/" +
          encodeURIComponent(commit) +
          "/file?path=" +
          encodeURIComponent(file),
      );
      if (selection !== state.versionGeneration || state.selectedFile !== file)
        return;
      $("version-file-content").textContent = result.binary
        ? "Binary file cannot be displayed."
        : (result.content || "") +
          (result.truncated ? "\n\n[File truncated]" : "");
    } catch (error) {
      if (selection === state.versionGeneration && state.selectedFile === file)
        $("version-file-content").textContent = error.message;
    }
  }
  document.querySelectorAll("[data-version-view]").forEach((button) =>
    button.addEventListener("click", () => {
      const view = button.dataset.versionView;
      document
        .querySelectorAll("[data-version-view]")
        .forEach((item) => item.classList.toggle("active", item === button));
      $("version-files-view").hidden = view !== "files";
      $("version-diff-view").hidden = view !== "diff";
    }),
  );
  function renderChat() {
    const box = $("chat-history");
    const active = state.chatProgress?.active[0];
    const pending = active || state.pendingChat;
    const entries = state.chatEntries;
    const optimistic = state.pendingChat;
    const showOptimistic =
      optimistic &&
      !entries
        .slice(optimistic.historyLength)
        .some(
          (entry) => entry.role === "user" && entry.text === optimistic.message,
        );
    const signature = JSON.stringify([
      entries,
      showOptimistic ? optimistic.message : null,
    ]);
    if (box.dataset.signature !== signature) {
      const nearBottom =
        box.scrollHeight - box.scrollTop - box.clientHeight < 80;
      const open = [...box.querySelectorAll("details")].map(
        (detail) => detail.open,
      );
      box.replaceChildren();
      entries.forEach((entry) => {
        if (entry.role === "tool") {
          const tool = JSON.parse(entry.text);
          const details = document.createElement("details");
          details.className = "chat-tool";
          box.appendChild(details);
          const result = tool.result;
          const verdict =
            result.passed === undefined
              ? result.ok === false
                ? "failed"
                : "completed"
              : result.passed
                ? "passed"
                : "failed";
          node("summary", tool.operation + " · " + verdict, details);
          node("pre", JSON.stringify(result, null, 2), details);
        } else {
          const turn = node("div", "", box);
          turn.className =
            "chat-turn " +
            (entry.role === "user" ? "chat-user" : "chat-assistant");
          node("strong", entry.role === "user" ? "You" : "Coding agent", turn);
          node("p", entry.text, turn);
        }
      });
      if (showOptimistic) {
        const turn = node("div", "", box);
        turn.className = "chat-turn chat-user";
        node("strong", "You", turn);
        node("p", optimistic.message, turn);
      }
      box.querySelectorAll("details").forEach((detail, index) => {
        detail.open = !!open[index];
      });
      box.dataset.signature = signature;
      if (nearBottom) box.scrollTop = box.scrollHeight;
    }
    $("chat-form").querySelector("button").disabled =
      !state.chatChecked || !!pending;
    $("agent-activity").classList.toggle("working", !!pending);
    if (pending) {
      const seconds = Math.max(
        0,
        Math.floor(Date.now() / 1000 - pending.updated_at),
      );
      status(
        "chat-status",
        `${pending.status === "running" ? "Coding agent is working" : "Sending to coding agent"} · ${seconds}s`,
      );
    } else if (state.chatError) {
      status("chat-status", state.chatError, true);
    } else if (state.chatProgress?.latest) {
      const latest = state.chatProgress.latest;
      status(
        "chat-status",
        latest.status === "completed"
          ? "Completed."
          : `${latest.status}${latest.error ? ": " + latest.error : ""}`,
        latest.status !== "completed",
      );
    } else {
      status(
        "chat-status",
        state.chatChecked ? "Ready for a request." : "Checking agent status…",
      );
    }
  }
  function renderRuns(expandId) {
    const box = $("runs");
    const open = new Set(
      [...box.querySelectorAll("details[open]")].map(
        (detail) => detail.dataset.run,
      ),
    );
    if (expandId) open.add(expandId);
    const statusFilter = $("run-status-filter").value;
    const scopeFilter = $("run-scope-filter").value;
    const signature = JSON.stringify([state.runs, statusFilter, scopeFilter]);
    if (!expandId && box.dataset.signature === signature) return;
    box.dataset.signature = signature;
    const visible = state.runs
      .filter(
        (run) =>
          (statusFilter === "all" || run.status === statusFilter) &&
          (scopeFilter === "all" ||
            (run.test_session_id ? "test" : "live") === scopeFilter),
      )
      .slice()
      .reverse();
    box.replaceChildren();
    if (!visible.length)
      node(
        "p",
        state.runs.length ? "No runs match these filters." : "No runs yet.",
        box,
      );
    visible.forEach((run) => {
      const details = document.createElement("details");
      details.className = "run-entry";
      details.id = "run-" + run.id;
      details.dataset.run = run.id;
      details.open = open.has(run.id);
      box.appendChild(details);
      const summary = node("summary", "", details);
      summary.className = "run-summary";
      node("strong", `${run.status} · ${run.id.slice(0, 8)}`, summary);
      node("span", run.test_session_id ? "Test" : "Live", summary);
      node(
        "p",
        `Run: ${run.id}\nWorkflow: ${run.workflow_id || "None"}\nCommit: ${run.commit || "None"}\nAttempt: ${run.attempt}\nScope: ${run.test_session_id ? "Test " + run.test_session_id : "Live"}`,
        details,
      );
      node("strong", "Event", details);
      node("pre", JSON.stringify(run.event, null, 2), details);
      if (run.logs?.length) {
        node("strong", "Logs", details);
        node(
          "pre",
          Array.isArray(run.logs) ? run.logs.join("\n") : String(run.logs),
          details,
        );
      }
      if (run.error) {
        node("strong", "Error", details);
        node("pre", String(run.error), details);
      }
    });
  }
  $("run-status-filter").addEventListener("change", () => renderRuns());
  $("run-scope-filter").addEventListener("change", () => renderRuns());
  function renderSession(session) {
    $("session").hidden = false;
    $("session-info").textContent =
      `Session ${session.id} · commit ${session.commit} · expires ${session.expires_at}`;
    const signature = JSON.stringify(session.timeline);
    if (signature === state.timeline) return;
    state.timeline = signature;
    const box = $("timeline");
    const responses = new Map(
      [...box.querySelectorAll("textarea[data-request]")].map((input) => [
        input.dataset.request,
        input.value,
      ]),
    );
    const focused = document.activeElement?.dataset?.request;
    box.replaceChildren();
    if (!session.timeline.length) node("p", "No mock requests yet.", box);
    session.timeline.forEach((item) => {
      const div = node("div", "", box);
      div.className = "entry";
      node("strong", `${item.operation} · ${item.status}`, div);
      node(
        "pre",
        JSON.stringify(
          { request: item.request, response: item.response },
          null,
          2,
        ),
        div,
      );
      if (item.status === "pending") {
        const form = document.createElement("form");
        const label = node("label", "Response (JSON object)", form);
        const input = document.createElement("textarea");
        input.dataset.request = item.id;
        input.value = responses.get(item.id) ?? '{"ok":true}';
        label.appendChild(input);
        const button = node("button", "Answer", form);
        div.appendChild(form);
        if (focused === item.id) input.focus();
        form.addEventListener("submit", (e) => {
          e.preventDefault();
          action(button, "test-status", async () => {
            await request(
              `/api/tests/${encodeURIComponent(state.session)}/prompts/${encodeURIComponent(item.id)}`,
              jsonObject(input.value),
            );
            status("test-status", "Response sent.");
            refresh();
          });
        });
      }
    });
  }
  function showView(view) {
    document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
      const active = item.dataset.view === view;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.panel !== view;
    });
    if (view === "agent")
      $("chat-history").scrollTop = $("chat-history").scrollHeight;
    const titles = {
      dashboard: "Dashboard",
      runs: "Runs",
      versions: "Versions",
      agent: "Coding agent",
      testing: "Test environment",
      analytics: "Analytics",
      integrations: "Integrations",
      settings: "Settings",
    };
    $("page-title").textContent = titles[view];
    $("breadcrumb-current").textContent = titles[view];
    $("page-description").textContent = state.projectName || "";
  }
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });
  $("projects").addEventListener("change", (e) =>
    selectProject(e.target.value),
  );
  $("create-project").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    action(form.querySelector("button"), "project-status", async () => {
      const p = await request("/api/projects", {
        name: form.elements.name.value,
      });
      form.reset();
      await projects(p.id);
      selectProject(p.id);
    });
  });
  $("example").addEventListener("click", (e) =>
    action(e.currentTarget, "project-action", async () => {
      const result = await request(path() + "/example", {});
      $("start-test").elements.commit.value = result.commit;
      $("main-target").value = result.commit;
      status(
        "project-action",
        "Example commit saved on " + result.branch + ".",
      );
      refresh();
    }),
  );
  $("update-main").addEventListener("click", (e) =>
    action(e.currentTarget, "project-action", async () => {
      const commit = $("main-target").value.trim();
      if (!commit) throw new Error("Choose a Git commit.");
      await request(path() + "/main", { commit });
      status("project-action", "Main now points to " + commit + ".");
      refresh();
    }),
  );
  $("chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const message = form.elements.message.value.trim();
    if (
      !message ||
      !state.chatChecked ||
      state.pendingChat ||
      state.chatProgress?.active.length
    )
      return;
    const generation = state.generation;
    state.chatError = "";
    state.pendingChat = {
      message,
      historyLength: state.chatEntries.length,
      status: "queued",
      updated_at: Date.now() / 1000,
    };
    form.elements.message.value = "";
    renderChat();
    $("chat-history").scrollTop = $("chat-history").scrollHeight;
    try {
      const result = await request(path() + "/chat", { message });
      if (generation !== state.generation) return;
      $("start-test").elements.commit.value = result.commit;
      $("main-target").value = result.commit;
      state.pendingChat = null;
      refresh();
    } catch (error) {
      if (generation !== state.generation) return;
      state.pendingChat = null;
      state.chatError = error.message;
      if (!form.elements.message.value) form.elements.message.value = message;
      state.chatProgress = {
        active: [],
        latest: { status: "failed", error: error.message },
      };
      renderChat();
      refresh();
    }
  });
  $("chat-message").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      $("chat-form").requestSubmit();
    }
  });
  $("start-test").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    action(form.querySelector("button"), "test-status", async () => {
      const body = { commit: form.elements.commit.value };
      if (form.elements.fixtures.value.trim())
        body.fixtures = jsonObject(form.elements.fixtures.value);
      const session = await request(path() + "/tests", body);
      state.session = session.id;
      state.timeline = "";
      renderSession(session);
      status("test-status", "Test started.");
      refresh();
    });
  });
  $("event-form").elements.kind.addEventListener("change", (e) => {
    $("comment-fields").hidden = e.target.value !== "comment";
  });
  $("event-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    action(form.querySelector("button"), "event-status", async () => {
      const f = form.elements;
      if (f.mode.value === "test" && !state.session)
        throw new Error("Start a test first.");
      const payload = { text: f.text.value, sender_id: f.sender_id.value };
      if (f.kind.value === "comment") {
        if (!f.comment_id.value || !f.post_id.value)
          throw new Error("Enter a comment ID and post ID.");
        payload.comment_id = f.comment_id.value;
        payload.post_id = f.post_id.value;
      }
      const event = {
        id: crypto.randomUUID(),
        kind: f.kind.value,
        conversation_id: f.conversation_id.value,
        payload,
      };
      await request(
        f.mode.value === "test"
          ? `/api/tests/${encodeURIComponent(state.session)}/events`
          : path() + "/debug/webhook",
        event,
      );
      status("event-status", "Event queued.");
      refresh();
    });
  });
  $("social-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    action(form.querySelector("button"), "social-status", async () => {
      await request(path() + "/debug/social", {
        operation: form.elements.operation.value,
        response: jsonObject(form.elements.response.value),
      });
      status("social-status", "Fixture saved.");
      refresh();
    });
  });
  request("/api/service")
    .then((catalog) => {
      const select = $("social-form").elements.operation;
      select.replaceChildren();
      Object.keys(catalog.operations).forEach((operation) =>
        select.add(new Option(operation, operation)),
      );
    })
    .catch((e) => status("social-status", e.message, true));
  let lastProject = "";
  try {
    lastProject = localStorage.getItem("self-coding-project") || "";
  } catch (_) {}
  projects(lastProject)
    .then((selected) => {
      $("connection-label").textContent = "Connected";
      if (selected) selectProject(selected);
    })
    .catch((e) => {
      $("connection-label").textContent = "Unavailable";
      status("project-status", e.message, true);
    });
  setInterval(() => {
    refresh();
    if (state.project) renderChat();
  }, 2000);
})();
