(function () {
  const configuredBackend = (window.EASY_CLAUDE_CONFIG && window.EASY_CLAUDE_CONFIG.backendUrl || "").trim();
  const backendCandidates = [configuredBackend, "", "http://127.0.0.1:8012", "http://127.0.0.1:8015", "http://127.0.0.1:8000"]
    .filter((value, index, array) => value && array.indexOf(value) === index);

  const state = {
    messages: [],
    streaming: false,
    config: null,
    showConfig: false,
    backendUrl: configuredBackend,
    history: [],
    abortController: null,
  };

  const ui = {
    modelPill: document.getElementById("model-pill"),
    configToggle: document.getElementById("config-toggle"),
    clearButton: document.getElementById("clear-button"),
    configPanel: document.getElementById("config-panel"),
    skillsList: document.getElementById("skills-list"),
    toolsList: document.getElementById("tools-list"),
    skillCount: document.getElementById("skill-count"),
    toolCount: document.getElementById("tool-count"),
    emptyState: document.getElementById("empty-state"),
    promptChips: document.getElementById("prompt-chips"),
    suggestionGrid: document.getElementById("suggestion-grid"),
    messagesList: document.getElementById("messages-list"),
    messagesEnd: document.getElementById("messages-end"),
    input: document.getElementById("composer-input"),
    sendButton: document.getElementById("send-button"),
    stopButton: document.getElementById("stop-button"),
  };

  const suggestionPrompts = [
    "计算 (2 + 3) * 7 并告诉我当前时间",
    "使用 code-review 技能分析一段代码",
    "生成一个叫 alice 的队友，角色是数据分析师",
    "创建一个任务：编写测试用例",
  ];

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function scrollToBottom() {
    ui.messagesEnd.scrollIntoView({ behavior: "smooth", block: "end" });
  }

  function autoResizeInput() {
    ui.input.style.height = "auto";
    ui.input.style.height = Math.min(ui.input.scrollHeight, 200) + "px";
  }

  function truncateLines(text, maxLines) {
    const lines = String(text).split("\n");
    return {
      preview: lines.slice(0, maxLines).join("\n"),
      hasMore: lines.length > maxLines,
      remaining: Math.max(lines.length - maxLines, 0),
    };
  }

  async function probeBackend(baseUrl) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 1500);
    try {
      const res = await fetch(`${baseUrl}/api/config`, { signal: controller.signal });
      return res.ok;
    } catch {
      return false;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function loadConfig() {
    for (const candidate of backendCandidates) {
      const ok = await probeBackend(candidate);
      if (!ok) continue;
      try {
        const res = await fetch(`${candidate}/api/config`);
        if (!res.ok) continue;
        const data = await res.json();
        state.backendUrl = candidate;
        state.config = data;
        render();
        return;
      } catch {
        // Continue probing.
      }
    }
    render();
  }

  function renderConfigPanel() {
    const config = state.config;
    if (!config) {
      ui.modelPill.classList.add("hidden");
      ui.configPanel.classList.add("hidden");
      return;
    }

    ui.modelPill.textContent = config.model || "";
    ui.modelPill.classList.remove("hidden");
    ui.configPanel.classList.toggle("hidden", !state.showConfig);

    const skills = Array.isArray(config.skills) ? config.skills : [];
    const tools = Array.isArray(config.mcp_tools) ? config.mcp_tools : [];
    ui.skillCount.textContent = `(${skills.length})`;
    ui.toolCount.textContent = `(${tools.length})`;

    ui.skillsList.innerHTML = skills
      .map((skill) => `<button class="chip-button skill-chip" data-fill="${escapeHtml(`使用 ${skill.name} 技能`)}" title="${escapeHtml(skill.description || "")}">${escapeHtml(skill.name)}</button>`)
      .join("");

    ui.toolsList.innerHTML = tools
      .map((tool) => `<button class="chip-button tool-chip" data-fill="${escapeHtml(`调用 ${tool.name} 工具`)}" title="${escapeHtml(tool.description || "")}">${escapeHtml(tool.name)}</button>`)
      .join("");
  }

  function renderEmptyState() {
    if (!state.config) {
      ui.promptChips.innerHTML = "";
    } else {
      const skillChips = (state.config.skills || []).map((skill) =>
        `<button class="chip-button skill-chip" data-fill="${escapeHtml(`使用 ${skill.name} 技能`)}">📚 ${escapeHtml(skill.name)}</button>`
      );
      const toolChips = (state.config.mcp_tools || []).map((tool) =>
        `<button class="chip-button tool-chip" data-fill="${escapeHtml(`调用 ${tool.name} 工具`)}">⚡ ${escapeHtml(tool.name)}</button>`
      );
      ui.promptChips.innerHTML = [...skillChips, ...toolChips].join("");
    }

    ui.suggestionGrid.innerHTML = suggestionPrompts
      .map((prompt) => `<button class="suggestion-button" data-fill="${escapeHtml(prompt)}">${escapeHtml(prompt)}</button>`)
      .join("");
  }

  function renderMarkdown(text) {
    const tokens = String(text).split(/(```[\s\S]*?```|`[^`]+`)/g);
    return tokens.map((part) => {
      if (!part) return "";
      if (part.startsWith("```") && part.endsWith("```")) {
        const firstNewline = part.indexOf("\n");
        const lang = firstNewline >= 0 ? part.slice(3, firstNewline).trim() : "";
        const code = firstNewline >= 0 ? part.slice(firstNewline + 1, -3).trim() : part.slice(3, -3).trim();
        const langHtml = lang ? `<div class="step-label">${escapeHtml(lang)}</div>` : "";
        return `<pre>${langHtml}<code>${escapeHtml(code)}</code></pre>`;
      }
      if (part.startsWith("`") && part.endsWith("`")) {
        return `<code class="inline">${escapeHtml(part.slice(1, -1))}</code>`;
      }
      return escapeHtml(part).replace(/\n/g, "<br>");
    }).join("");
  }

  function renderJsonBlock(data) {
    const text = typeof data === "string" ? data : JSON.stringify(data, null, 2);
    const truncated = truncateLines(text, 8);
    const preview = escapeHtml(truncated.preview);
    if (!truncated.hasMore) {
      return `<pre class="json-box">${preview}</pre>`;
    }
    return [
      `<div class="json-block">`,
      `<pre class="json-box">${preview}</pre>`,
      `<button class="ghost-button tool-expand" data-expand='${escapeHtml(text)}'>显示更多 (${truncated.remaining} 行)...</button>`,
      `</div>`,
    ].join("");
  }

  function renderToolCard(step, result) {
    const typeClass = `tool-${step.tool_type || "builtin"}`;
    const tagLabel = step.tool_type === "mcp" ? "MCP" : step.tool_type === "skill" ? "Skill" : "内置";
    const status = !result ? "⏳" : result.error ? "✖" : "✔";
    const bodyParts = [];

    if (step.input && Object.keys(step.input).length > 0) {
      bodyParts.push(`<div><div class="step-label">输入</div>${renderJsonBlock(step.input)}</div>`);
    }
    if (result) {
      bodyParts.push(`<div><div class="step-label">${result.error ? "错误" : "输出"}</div>${renderJsonBlock(result.content || "")}</div>`);
    } else {
      bodyParts.push(`<div class="system-event">执行中...</div>`);
    }

    return [
      `<div class="step-card ${typeClass}">`,
      `<button class="step-header" data-toggle-step>`,
      `<span class="step-chevron">▸</span>`,
      `<span class="step-title">${escapeHtml(step.name)}</span>`,
      `<span class="step-tag">${tagLabel}</span>`,
      `<span class="step-status">${status}</span>`,
      `</button>`,
      `<div class="step-body hidden">${bodyParts.join("")}</div>`,
      `</div>`,
    ].join("");
  }

  function renderTeamCard(step) {
    const labelMap = {
      spawned: `队友 ${step.teammate || ""} 已生成（${step.role || ""}）`,
      idle: `${step.teammate || ""} 进入空闲`,
      shutdown_request: `请求关闭 ${step.teammate || ""}`,
      shutdown_complete: `${step.teammate || ""} 已关闭`,
      shutdown_timeout: `${step.teammate || ""} 超时关闭`,
      plan_submitted: `${step.teammate || ""} 提交计划等待审批`,
      plan_reviewed: `计划${step.approved ? "已批准" : "已拒绝"}（${step.teammate || ""}）`,
      task_claimed: `${step.teammate || ""} 认领任务 #${step.task_id || ""}`,
      tool_call: `${step.teammate || ""}: 调用 ${step.tool || ""}`,
      error: `${step.teammate || ""} 出错`,
    };
    const label = labelMap[step.event] || `团队事件: ${step.event}`;
    const details = [];
    if (step.plan) details.push(`<div><div class="step-label">计划内容</div>${renderJsonBlock(step.plan)}</div>`);
    if (step.feedback) details.push(`<div><div class="step-label">反馈</div>${renderJsonBlock(step.feedback)}</div>`);
    if (step.output_preview) details.push(`<div><div class="step-label">输出预览</div>${renderJsonBlock(step.output_preview)}</div>`);
    if (step.error) details.push(`<div><div class="step-label">错误</div>${renderJsonBlock(step.error)}</div>`);
    const expandable = details.length > 0;

    return [
      `<div class="step-card team">`,
      `<button class="step-header"${expandable ? " data-toggle-step" : ""}>`,
      `<span class="step-chevron">${expandable ? "▸" : ""}</span>`,
      `<span>${escapeHtml(label)}</span>`,
      `${step.request_id ? `<span class="step-status">#${escapeHtml(step.request_id)}</span>` : ""}`,
      `</button>`,
      expandable ? `<div class="step-body hidden">${details.join("")}</div>` : "",
      `</div>`,
    ].join("");
  }

  function renderSystemEvent(step) {
    return `<div class="system-event">⚙ ${escapeHtml(step.message || "")}</div>`;
  }

  function renderErrorEvent(step) {
    return `<div class="error-inline">✖ ${escapeHtml(step.message || "")}</div>`;
  }

  function renderSteps(steps) {
    if (!steps || !steps.length) return "";

    const resultMap = new Map();
    steps.forEach((event) => {
      if (event.type === "tool_result") resultMap.set(event.id, event);
    });

    const usedResults = new Set();
    const parts = [];

    steps.forEach((step) => {
      if (step.type === "tool_call") {
        const result = resultMap.get(step.id);
        if (result) usedResults.add(step.id);
        parts.push(renderToolCard(step, result));
        return;
      }
      if (step.type === "tool_result" && !usedResults.has(step.id)) {
        parts.push(`<div class="system-event">结果: ${escapeHtml(String(step.content || "").slice(0, 100))}</div>`);
        return;
      }
      if (step.type === "team_event") {
        parts.push(renderTeamCard(step));
        return;
      }
      if (step.type === "system_event") {
        parts.push(renderSystemEvent(step));
        return;
      }
      if (step.type === "error") {
        parts.push(renderErrorEvent(step));
      }
    });

    return parts.length ? `<div class="steps-list">${parts.join("")}</div>` : "";
  }

  function renderMessages() {
    const hasMessages = state.messages.length > 0;
    ui.emptyState.classList.toggle("hidden", hasMessages);
    ui.messagesList.classList.toggle("hidden", !hasMessages);
    ui.clearButton.classList.toggle("hidden", !hasMessages);

    if (!hasMessages) {
      ui.messagesList.innerHTML = "";
      renderEmptyState();
      return;
    }

    ui.messagesList.innerHTML = state.messages.map((message) => {
      if (message.role === "user") {
        return `<div class="message-row user"><div class="message-user">${escapeHtml(message.content)}</div></div>`;
      }

      const meta = [
        `<div class="assistant-meta">`,
        `<div class="assistant-avatar">A</div>`,
        `<span>Agent</span>`,
        message.streaming ? `<span class="assistant-thinking">• 思考中...</span>` : "",
        `</div>`,
      ].join("");

      const steps = renderSteps(message.steps || []);
      const content = message.content
        ? `<div class="message-card">${renderMarkdown(message.content)}${message.streaming ? '<span class="cursor-blink"></span>' : ''}</div>`
        : message.streaming
          ? `<div class="message-card"><div class="typing-indicator"><span></span><span></span><span></span></div></div>`
          : "";

      return `<div class="message-row assistant"><div class="message-assistant">${meta}${steps}${content}</div></div>`;
    }).join("");

    scrollToBottom();
  }

  function renderComposer() {
    ui.sendButton.disabled = state.streaming || !ui.input.value.trim();
    ui.input.disabled = state.streaming;
    ui.stopButton.classList.toggle("hidden", !state.streaming);
    ui.sendButton.classList.toggle("hidden", state.streaming);
  }

  function render() {
    renderConfigPanel();
    renderMessages();
    renderComposer();
    autoResizeInput();
  }

  function setInputValue(text) {
    ui.input.value = text;
    autoResizeInput();
    renderComposer();
    ui.input.focus();
  }

  function updateAssistantMessage(messageId, patch) {
    state.messages = state.messages.map((message) => message.id === messageId ? { ...message, ...patch } : message);
    renderMessages();
  }

  async function sendMessage() {
    const text = ui.input.value.trim();
    if (!text || state.streaming || !state.backendUrl) return;

    const userId = String(Date.now());
    const assistantId = String(Date.now() + 1);
    state.messages.push({ id: userId, role: "user", content: text });
    state.messages.push({ id: assistantId, role: "assistant", content: "", steps: [], streaming: true });
    ui.input.value = "";
    state.streaming = true;
    render();

    let accContent = "";
    const accSteps = [];
    const controller = new AbortController();
    state.abortController = controller;

    try {
      const response = await fetch(`${state.backendUrl}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history: state.history }),
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        lines.forEach((line) => {
          if (!line.startsWith("data: ")) return;
          const jsonText = line.slice(6).trim();
          if (!jsonText) return;
          let event;
          try {
            event = JSON.parse(jsonText);
          } catch {
            return;
          }

          switch (event.type) {
            case "message_delta":
              accContent += event.content || "";
              updateAssistantMessage(assistantId, { content: accContent, steps: accSteps.slice() });
              break;
            case "message_done":
              accContent = event.content || "";
              updateAssistantMessage(assistantId, { content: accContent, steps: accSteps.slice(), streaming: false });
              break;
            case "tool_call":
            case "tool_result":
            case "team_event":
            case "system_event":
            case "error":
              accSteps.push(event);
              updateAssistantMessage(assistantId, { steps: accSteps.slice() });
              break;
            case "done":
              updateAssistantMessage(assistantId, { streaming: false, steps: accSteps.slice() });
              break;
            case "history_update":
              if (Array.isArray(event.history)) {
                state.history = event.history;
              }
              break;
            default:
              break;
          }
        });
      }
    } catch (error) {
      const errorMessage = error && error.name === "AbortError" ? "已停止生成" : (error && error.message) || "未知错误";
      accSteps.push({ type: "error", message: errorMessage });
      updateAssistantMessage(assistantId, { steps: accSteps.slice(), streaming: false });
    } finally {
      state.abortController = null;
      state.streaming = false;
      state.messages = state.messages.map((message) => message.id === assistantId ? { ...message, streaming: false } : message);
      render();
      ui.input.focus();
    }
  }

  function stopStreaming() {
    if (state.abortController) state.abortController.abort();
    state.streaming = false;
    state.messages = state.messages.map((message) => message.streaming ? { ...message, streaming: false } : message);
    render();
  }

  function clearHistory() {
    state.messages = [];
    state.history = [];
    render();
  }

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;

    const fillButton = target.closest("[data-fill]");
    if (fillButton) {
      setInputValue(fillButton.getAttribute("data-fill") || "");
      return;
    }

    const expandButton = target.closest("[data-expand]");
    if (expandButton) {
      const text = expandButton.getAttribute("data-expand") || "";
      const wrapper = expandButton.closest(".json-block");
      if (wrapper) {
        wrapper.innerHTML = `<pre class="json-box">${escapeHtml(text)}</pre>`;
      }
      return;
    }

    const toggle = target.closest("[data-toggle-step]");
    if (toggle) {
      const card = toggle.parentElement;
      if (!card) return;
      const body = card.querySelector(".step-body");
      const chevron = card.querySelector(".step-chevron");
      if (!body || !chevron) return;
      body.classList.toggle("hidden");
      chevron.textContent = body.classList.contains("hidden") ? "▸" : "▾";
    }
  });

  ui.configToggle.addEventListener("click", () => {
    state.showConfig = !state.showConfig;
    renderConfigPanel();
  });

  ui.clearButton.addEventListener("click", clearHistory);
  ui.sendButton.addEventListener("click", sendMessage);
  ui.stopButton.addEventListener("click", stopStreaming);

  ui.input.addEventListener("input", () => {
    autoResizeInput();
    renderComposer();
  });

  ui.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });

  render();
  loadConfig();
})();
