const statusLabels = {
  not_requested: "未依頼",
  requested: "依頼済",
  received: "受領済",
  verified: "確認済",
};

const statusOrder = ["not_requested", "requested", "received", "verified"];

let currentPayload = null;
let editingHeirId = null;
let renderedClarificationRunId = "";

const HEIR_RELATIONSHIP_BY_NAME = {
  "配偶者": "spouse",
  "長男": "eldest_son",
  "長女": "eldest_daughter",
  "次男": "second_son",
  "次女": "second_daughter",
  "三男": "third_son",
  "三女": "third_daughter",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    throw new Error(apiErrorMessage(response.status, body));
  }
  return response.json();
}

async function refresh() {
  currentPayload = await api("/api/case");
  render(currentPayload);
}

function render(payload) {
  restoreInteractionLocks();
  const { analysis, rules_summary, counterfactuals, harness, last_run, approval } = payload;
  renderCase(analysis);
  renderControls(payload.state, rules_summary, analysis);
  renderApproval(approval, payload.state, last_run);
  renderRun(last_run, approval);
  renderGeminiEvidence(last_run);
  renderClarification(last_run);
  renderMetrics(analysis, harness);
  renderKanban(analysis, rules_summary);
  renderDraft(analysis, payload.manual_inputs || {});
  renderBranches(counterfactuals);
  renderHarness(harness);
  renderPendingLock(last_run);
  renderExecutionLock(isRunning);
}

function renderApproval(approval, state, lastRun) {
  const enabled = Boolean(approval && approval.word_export_enabled);
  const ready = Boolean(approval && approval.review_ready);
  const awaiting = Boolean(lastRun && lastRun.status === "AWAITING_CLARIFICATION");
  const cardReady = Boolean(state && Array.isArray(state.heirs) && state.heirs.length > 0 && state.home_acquirer_id);
  const approveButton = qs("#approveButton");
  const inlineApproveButton = qs("#inlineApproveButton");
  const cardReviewButton = qs("#cardReviewButton");
  const wordLink = qs("#wordLink");
  const inlineWordLink = qs("#inlineWordLink");
  const approvalHint = qs("#approvalHint");
  const wordHint = qs("#wordHint");
  const flowStatus = qs("#wordFlowStatus");
  const flowHint = qs("#wordFlowHint");
  qs("#runButton").disabled = awaiting;
  const approvalText = awaiting
    ? "追加情報待ち"
    : enabled
      ? "レビュー完了済"
      : ready
        ? "レビュー完了（承認）"
        : "Review作成待ち";
  approveButton.textContent = approvalText;
  inlineApproveButton.textContent = enabled ? "② レビュー完了済" : ready ? "② レビュー完了（承認）" : "② レビュー完了（承認）";
  approveButton.disabled = enabled || !ready;
  inlineApproveButton.disabled = enabled || !ready;
  cardReviewButton.disabled = awaiting || ready || enabled || !cardReady;
  cardReviewButton.textContent = ready || enabled
    ? "Review作成済み"
    : cardReady
      ? "カード内容でReview作成"
      : "カード登録後にReview作成";
  [wordLink, inlineWordLink].forEach((link) => {
    link.classList.toggle("disabled", !enabled);
    link.setAttribute("aria-disabled", enabled ? "false" : "true");
  });
  approvalHint.textContent = awaiting
    ? "追加回答後にReview作成へ進みます"
    : enabled
    ? "レビュー完了済みです"
    : ready
      ? "内容確認後、レビュー完了（承認）してください"
      : cardReady
        ? "カード内容でReview作成、または相談文を実行してください"
        : "相続人カードを登録するとReview作成できます";
  wordHint.textContent = enabled ? "クリックしてWordを出力できます" : "レビュー完了するとWord出力できます";
  flowStatus.textContent = awaiting
    ? "追加情報待ちで安全停止"
    : enabled
    ? "③ Word出力できます"
    : ready
      ? "② レビュー完了（承認）"
      : cardReady
        ? "① Review作成できます"
        : "① 相続人カードを登録";
  flowHint.textContent = awaiting
    ? "Geminiの質問に回答すると、同じ相談を引き継いでRouter判断を再開します。"
    : enabled
    ? "右のWord出力ボタンを押すと、書面添付資料のdocxがダウンロードされます。"
    : ready
      ? "アラート、不足資料、総合所見を確認したら「② レビュー完了（承認）」を押します。"
      : cardReady
        ? "相談文なしで進める場合は「カード内容でReview作成」を押します。"
        : "相続人カードを登録すると、相談文なしでもReview作成できます。";
}

function renderRun(run, approval) {
  const timeline = qs("#runTimeline");
  const mode = qs("#runMode");
  timeline.innerHTML = "";
  if (!run) {
    timeline.style.setProperty("--timeline-columns", 1);
    mode.textContent = "決定的リプレイ";
    const empty = document.createElement("div");
    empty.className = "timeline-step";
    empty.innerHTML = "<strong>待機中</strong><p>「① AIエージェントを実行」、またはカード内容でReview作成するとACTIONタイムラインを表示します。</p>";
    timeline.appendChild(empty);
    return;
  }
  timeline.style.setProperty("--timeline-columns", Math.min(run.steps.length, 5));
  mode.textContent = runModeLabel(run);
  const approved = Boolean(approval && approval.word_export_enabled);
  run.steps.forEach((step) => timeline.appendChild(timelineStep(step, approved)));
}

function renderGeminiEvidence(run) {
  const box = qs("#geminiEvidence");
  box.innerHTML = "";
  if (!run || !run.gemini) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const g = run.gemini;
  const used = Boolean(g.used);
  box.className = `gemini-evidence ${used ? "live" : g.configured ? "fallback" : "local"}`;

  const head = document.createElement("div");
  head.className = "gemini-evidence-head";
  const label = document.createElement("strong");
  label.textContent = used
    ? "Gemini 実行トレース（Function Calling）"
    : g.configured
      ? "Gemini fallback（決定的コアで継続）"
      : "Gemini 未接続（決定的リプレイ）";
  const badge = document.createElement("span");
  badge.className = `gemini-badge ${used ? "green" : "amber"}`;
  badge.textContent = used ? "実接続" : "fallback";
  head.append(label, badge);
  box.appendChild(head);

  const result = g.arguments && g.arguments.acquirer_type
    ? g.arguments.acquirer_type
    : g.arguments && g.arguments.missing_fact
      ? g.arguments.missing_fact
      : "—";
  const rows = document.createElement("dl");
  rows.className = "gemini-evidence-rows";
  const entries = [
    ["model", g.model || "—"],
    ["tool", used ? g.tool_name || "—" : "—"],
    ["result", used ? result : "—"],
    ["latency", `${Number(g.latency_ms || 0)}ms`],
    ["fallback", used ? "なし" : fallbackLabel(g.fallback_reason) || "—"],
  ];
  entries.forEach(([key, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    rows.append(dt, dd);
  });
  box.appendChild(rows);

  if (g.guardrail_applied) {
    const guardrail = document.createElement("p");
    guardrail.className = "gemini-guardrail";
    const proposal = g.proposed_route || g.tool_name || "—";
    guardrail.textContent = `Gemini提案 ${proposal} → 決定的ガード後 ${g.effective_route || "継続"}：${g.guardrail_reason}`;
    box.appendChild(guardrail);
  }

  if (used && g.arguments && g.arguments.reason) {
    const reason = document.createElement("p");
    reason.className = "gemini-reason";
    reason.textContent = `理由: ${g.arguments.reason}`;
    box.appendChild(reason);
  }

  renderDecisionHistory(box, run.decision_history || []);
}

function renderDecisionHistory(container, history) {
  if (!Array.isArray(history) || history.length === 0) return;
  const section = document.createElement("div");
  section.className = "decision-history";
  const title = document.createElement("strong");
  title.textContent = history.length > 1 ? "判断履歴（停止 → 再開）" : "判断履歴";
  const list = document.createElement("ol");
  history.forEach((event) => {
    const item = document.createElement("li");
    const mutation = event.state_mutated ? "状態更新" : "状態変更なしで停止";
    item.textContent = `${event.tool} → ${event.result} / ${mutation}`;
    list.appendChild(item);
  });
  section.append(title, list);
  container.appendChild(section);
}

function renderClarification(run) {
  const panel = qs("#clarificationPanel");
  const awaiting = Boolean(run && run.status === "AWAITING_CLARIFICATION" && run.clarification);
  panel.hidden = !awaiting;
  if (!awaiting) {
    renderedClarificationRunId = "";
    qs("#clarificationAnswer").value = "";
    setClarificationError("");
    return;
  }
  if (renderedClarificationRunId !== run.id) {
    qs("#clarificationAnswer").value = "";
    renderedClarificationRunId = run.id;
  }
  qs("#clarificationQuestion").textContent = run.clarification.question;
}

function renderPendingLock(run) {
  const locked = Boolean(run && run.status === "AWAITING_CLARIFICATION");
  const selectors = [
    "#consultationText",
    "#partitionSelect",
    "#heirRegistrationForm select",
    "#heirRegistrationForm button",
    "#heirBoard input",
    "#heirBoard button",
    "#kanbanGrid select",
    "#overallOpinionInput",
    "#saveOverallOpinionButton",
    "#clearOverallOpinionButton",
  ];
  document.querySelectorAll(selectors.join(", ")).forEach((control) => {
    if (!locked) return;
    rememberInteractionBase(control);
    control.disabled = true;
    control.setAttribute("aria-disabled", "true");
  });
}

function renderExecutionLock(locked) {
  const selectors = [
    "#seedButton",
    "#ambiguousDemoButton",
    "#consultationText",
    "#clarificationAnswer",
    "#partitionSelect",
    "#heirRegistrationForm select",
    "#heirRegistrationForm button",
    "#heirBoard input",
    "#heirBoard button",
    "#kanbanGrid select",
    "#overallOpinionInput",
    "#saveOverallOpinionButton",
    "#clearOverallOpinionButton",
    "#approveButton",
    "#inlineApproveButton",
  ];
  document.querySelectorAll(selectors.join(", ")).forEach((control) => {
    if (!locked) return;
    rememberInteractionBase(control);
    control.disabled = true;
    control.setAttribute("aria-disabled", "true");
  });
  [qs("#wordLink"), qs("#inlineWordLink")].forEach((link) => {
    if (!locked) return;
    rememberLinkBase(link);
    link.classList.add("disabled");
    link.setAttribute("aria-disabled", "true");
  });
}

function rememberInteractionBase(control) {
  if (!Object.prototype.hasOwnProperty.call(control.dataset, "lockBaseDisabled")) {
    control.dataset.lockBaseDisabled = control.disabled ? "true" : "false";
  }
}

function rememberLinkBase(link) {
  if (!Object.prototype.hasOwnProperty.call(link.dataset, "lockBaseLinkDisabled")) {
    link.dataset.lockBaseLinkDisabled = link.classList.contains("disabled") ? "true" : "false";
    link.dataset.lockBaseAriaDisabled = link.getAttribute("aria-disabled") || "false";
  }
}

function restoreInteractionLocks() {
  document.querySelectorAll("[data-lock-base-disabled]").forEach((control) => {
    control.disabled = control.dataset.lockBaseDisabled === "true";
    control.setAttribute("aria-disabled", control.disabled ? "true" : "false");
    delete control.dataset.lockBaseDisabled;
  });
  document.querySelectorAll("[data-lock-base-link-disabled]").forEach((link) => {
    link.classList.toggle("disabled", link.dataset.lockBaseLinkDisabled === "true");
    link.setAttribute("aria-disabled", link.dataset.lockBaseAriaDisabled || "false");
    delete link.dataset.lockBaseLinkDisabled;
    delete link.dataset.lockBaseAriaDisabled;
  });
}

function fallbackLabel(reason) {
  const map = {
    gemini_api_key_not_set: "APIキー未設定（決定的リプレイ）",
    gemini_no_function_call: "function call無し→決定的コアで継続",
    gemini_invalid_tool_call: "無効なtool呼び出し→決定的コアで継続",
    clarification_not_needed_structured_facts: "構造化事実を優先→決定的コアで継続",
  };
  return map[reason] || (reason ? `${reason}→決定的コアで継続` : "");
}

function runModeLabel(run) {
  if (run.mode === "gemini_function_calling") {
    return "Gemini 3.5 Flash 実接続";
  }
  if (run.mode === "deterministic_safe_stop") {
    return "決定的ガードで安全停止";
  }
  if (run.gemini_configured) {
    return "Gemini fallback（決定的）";
  }
  return "決定的リプレイ";
}

function timelineStep(step, approved) {
  const isReview = step.id === "review";
  const reviewDone = isReview && Boolean(approved);
  const pending = step.status === "PENDING_APPROVAL" && !reviewDone;
  const awaiting = step.status === "AWAITING_INPUT";

  const item = document.createElement("article");
  item.className = `timeline-step ${pending ? "pending" : ""} ${awaiting ? "awaiting" : ""} ${reviewDone ? "approved" : ""}`.trim();
  const title = document.createElement("strong");
  title.textContent = step.label;
  const badge = document.createElement("span");
  badge.className = `badge ${pending ? "red" : awaiting ? "amber" : "green"}`;
  badge.textContent = pending
    ? "レビュー完了待ち"
    : awaiting
      ? "追加情報待ち"
      : reviewDone
        ? "レビュー完了"
        : "DONE";
  title.appendChild(badge);

  const summary = document.createElement("p");
  summary.textContent = reviewDone
    ? "税理士がレビュー完了（承認）しました。書面添付資料をWord出力できます。"
    : step.summary;
  const actions = document.createElement("ul");
  step.actions.slice(0, 4).forEach((action) => {
    const li = document.createElement("li");
    li.textContent = actionSummary(action);
    actions.appendChild(li);
  });
  const secondaryPrompt = step.actions.find((action) => action.type === "ask_secondary_inheritance_review");
  const eligibilityAlerts = step.actions.filter((action) => action.type === "alert_small_residence_ineligible");
  const approvalAction = step.actions.find((action) => action.type === "request_human_approval");
  item.append(title, summary, actions);
  eligibilityAlerts.forEach((action) => item.appendChild(eligibilityAlertCard(action)));
  if (secondaryPrompt) {
    item.appendChild(secondaryPromptCard(secondaryPrompt));
  }
  if (approvalAction) {
    item.appendChild(reviewCompletionCard(approvalAction, reviewDone));
  }
  return item;
}

function reviewCompletionCard(action, done) {
  const card = document.createElement("div");
  card.className = `review-completion-card ${done ? "done" : ""}`.trim();
  const title = document.createElement("strong");
  title.textContent = done ? "レビュー完了（承認済み）" : "レビュー完了にする操作";
  const message = document.createElement("p");
  message.textContent = done
    ? "税理士が承認しました。承認後だけ書面添付資料をWord出力できます。"
    : action.reason;
  card.append(title, message);
  return card;
}

function eligibilityAlertCard(action) {
  const card = document.createElement("div");
  card.className = "eligibility-alert-card";
  const title = document.createElement("strong");
  title.textContent = action.value;
  const impact = document.createElement("div");
  impact.className = "review-impact";
  const impactLabel = document.createElement("span");
  impactLabel.textContent = "否認インパクト";
  const impactValue = document.createElement("b");
  impactValue.textContent = taxablePriceImpact(action.impact_yen);
  impact.append(impactLabel, impactValue);
  const message = document.createElement("p");
  message.textContent = `${action.message} ${action.impact_summary || ""}`.trim();
  card.append(title, impact, message);
  return card;
}

function secondaryPromptCard(action) {
  const card = document.createElement("div");
  card.className = "secondary-prompt";
  const title = document.createElement("strong");
  title.textContent = action.value;
  const why = document.createElement("p");
  why.textContent = action.why;
  card.append(title, why);
  return card;
}

function renderCase(analysis) {
  qs("#caseId").textContent = analysis.case.id;
  qs("#caseTitle").textContent = analysis.case.title;
  qs("#caseLand").textContent = `${analysis.case.land.name} ${analysis.case.land.area_sqm}㎡ / 架空評価 ${yen(analysis.case.land.estimated_value_yen)}`;
}

function renderControls(state, summary, analysis) {
  const board = qs("#heirBoard");
  board.innerHTML = "";
  const heirs = state.heirs || [];
  if (heirs.length === 0) {
    const empty = document.createElement("article");
    empty.className = "heir-card empty";
    empty.innerHTML = `
      <span class="heir-relation">未登録</span>
      <strong>相続人カード未登録</strong>
      <p>相談文から候補カードを起票待ち</p>
    `;
    board.appendChild(empty);
  }
  heirs.forEach((heir) => {
    const card = document.createElement("article");
    const selected = heir.id === state.home_acquirer_id;
    card.className = `heir-card ${selected ? "selected" : ""}`;
    card.dataset.heirId = heir.id;

    const relation = document.createElement("span");
    relation.className = "heir-relation";
    relation.textContent = heir.relation_label || (heir.relation === "spouse" ? "配偶者" : "子");

    const name = document.createElement("strong");
    name.textContent = heir.name;

    const status = document.createElement("span");
    status.className = `heir-status ${heir.co_resident ? "co" : "separate"}`;
    status.textContent = heir.relation === "spouse" ? "配偶者" : heir.co_resident ? "同居" : "別居";

    const choose = document.createElement("button");
    choose.type = "button";
    choose.dataset.action = "choose-home-acquirer";
    choose.textContent = selected ? "自宅取得者" : "自宅を取得";
    choose.disabled = selected;
    choose.addEventListener("click", () => {
      patchCase({ home_acquirer_id: heir.id }).catch((error) => setRunError(error.message));
    });

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.dataset.action = "toggle-co-resident";
    toggle.textContent = heir.co_resident ? "同居" : "別居";
    toggle.className = "co-toggle";
    toggle.disabled = heir.relation === "spouse";
    toggle.addEventListener("click", () => {
      patchHeir(heir.id, { co_resident: !heir.co_resident }).catch((error) => setRunError(error.message));
    });

    const actions = document.createElement("div");
    actions.className = "heir-card-actions";

    const edit = document.createElement("button");
    edit.type = "button";
    edit.dataset.action = "edit-heir";
    edit.textContent = "修正";
    edit.addEventListener("click", () => beginHeirEdit(heir));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.dataset.action = "delete-heir";
    remove.className = "danger-button";
    remove.textContent = "削除";
    remove.addEventListener("click", () => {
      if (!window.confirm(`${heir.name}を削除しますか？`)) return;
      deleteHeir(heir.id).catch((error) => setHeirError(error.message));
    });

    actions.append(edit, remove);

    const meta = document.createElement("p");
    meta.textContent = selected
      ? `${analysis.acquirer.label}として要件確認中`
      : "クリックで自宅取得者に設定";

    card.append(relation, name, status, choose, toggle, actions, meta);
    board.appendChild(card);
  });

  const select = qs("#partitionSelect");
  select.innerHTML = "";
  summary.partition_statuses.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.label;
    option.selected = item.id === state.partition_status;
    select.appendChild(option);
  });
}

function renderMetrics(analysis, harness) {
  qs("#completionPercent").textContent = `${analysis.completion.percent}%`;
  qs("#completionFill").style.width = `${analysis.completion.percent}%`;
  qs("#completionLabel").textContent = analysis.completion.label;

  const alerts = analysis.eligibility_alerts || [];
  const currentImpactYen = alerts.reduce((sum, alert) => sum + Number(alert.impact_yen || 0), 0);
  const hasCurrentImpact = currentImpactYen > 0;

  const pill = qs("#harnessPill");
  pill.classList.toggle("green", !hasCurrentImpact && harness.ok);
  pill.classList.toggle("red", hasCurrentImpact || !harness.ok);
  qs("#harnessImpactLabel").textContent = hasCurrentImpact ? "現在案件インパクト" : "検証ハーネス";
  pill.querySelector("strong").textContent = hasCurrentImpact ? "要確認" : harness.ok ? "全検査OK" : "赤あり";
  qs("#harnessImpactValue").textContent = taxablePriceImpact(
    hasCurrentImpact ? currentImpactYen : harness.total_damage_yen,
  );
  qs("#harnessImpactNote").textContent = hasCurrentImpact
    ? "適用不可アラートの課税価格影響です。"
    : harness.tax_formula_note;

  const eligibility = qs("#eligibilityPill");
  const secondaryAlert = analysis.secondary_inheritance_alert;
  const hasEligibilityAlert = alerts.length > 0;
  const hasSecondaryAlert = !hasEligibilityAlert && Boolean(secondaryAlert);
  const impactBox = qs("#eligibilityImpact");
  eligibility.classList.toggle("red", hasEligibilityAlert);
  eligibility.classList.toggle("amber", hasSecondaryAlert);
  eligibility.classList.toggle("green", !hasEligibilityAlert && !hasSecondaryAlert);
  qs("#eligibilityAlertLabel").textContent = hasEligibilityAlert
    ? "適用不可アラート"
    : hasSecondaryAlert
      ? "二次相続アラート"
      : "適用可否アラート";
  eligibility.querySelector("strong").textContent = hasEligibilityAlert
    ? alerts[0].title
    : hasSecondaryAlert
      ? secondaryAlert.title
      : "通常確認";
  eligibility.querySelector("p").textContent = hasEligibilityAlert
    ? alerts[0].message
    : hasSecondaryAlert
      ? "配偶者取得時は一次・二次相続を通算して税理士が確認します。"
      : `${analysis.home_acquirer?.name || "取得者"}を自宅取得者として確認中。`;
  impactBox.hidden = !hasEligibilityAlert;
  if (hasEligibilityAlert) {
    qs("#eligibilityImpactValue").textContent = taxablePriceImpact(alerts[0].impact_yen);
  }
}

function renderKanban(analysis, summary) {
  const grid = qs("#kanbanGrid");
  const required = new Set(analysis.acquirer.required_document_ids);
  const groups = Object.fromEntries(statusOrder.map((status) => [status, []]));
  analysis.documents.forEach((doc) => groups[doc.status].push(doc));

  grid.innerHTML = "";
  statusOrder.forEach((status) => {
    const column = document.createElement("section");
    column.className = "kanban-column";
    column.innerHTML = `<h3><span>${statusLabels[status]}</span><span>${groups[status].length}</span></h3>`;
    groups[status].forEach((doc) => column.appendChild(documentCard(doc, required.has(doc.id), summary.document_statuses)));
    grid.appendChild(column);
  });
  qs("#missingCount").textContent = `不足${analysis.missing_documents.length}件`;
}

function documentCard(doc, required, statuses) {
  const card = document.createElement("article");
  card.className = `doc-card ${required ? "required" : ""}`;
  const select = document.createElement("select");
  statuses.forEach((status) => {
    const option = document.createElement("option");
    option.value = status;
    option.textContent = statusLabels[status];
    option.selected = status === doc.status;
    select.appendChild(option);
  });
  select.addEventListener("change", () => {
    patchDocument(doc.id, select.value).catch((error) => setRunError(error.message));
  });
  card.innerHTML = `<strong>${doc.label}</strong><p>${doc.reason}</p>`;
  card.appendChild(select);
  return card;
}

function renderDraft(analysis, manualInputs) {
  qs("#draftStatus").textContent = analysis.draft.status_label;
  list(qs("#presentedDocs"), analysis.draft.section_1_presented_documents);
  list(qs("#landReview"), analysis.draft.section_3_land_review);
  const opinion = manualInputs.overall_opinion || "";
  qs("#overallOpinionInput").value = opinion;
  qs("#overallOpinionStatus").textContent = opinion ? "保存済" : "未入力";
  list(qs("#nextActions"), analysis.next_actions);
}

function renderBranches(branches) {
  const grid = qs("#branchGrid");
  grid.innerHTML = "";
  branches.forEach((branch) => {
    const item = document.createElement("article");
    item.className = "branch-item";
    item.innerHTML = `
      <h3>${branch.label}<span class="badge green">${branch.completion_percent}%</span></h3>
      <p>不足: ${branch.missing_document_labels.slice(0, 4).join(" / ") || "なし"}</p>
      <p>${branch.land_review.slice(0, 3).join(" ")}</p>
    `;
    grid.appendChild(item);
  });
}

function renderHarness(harness) {
  qs("#damageTotal").textContent = `現行 否認インパクト ${taxablePriceImpact(harness.total_damage_yen)}`;
  const listEl = qs("#testList");
  listEl.innerHTML = "";
  harness.results.forEach((result) => {
    const item = document.createElement("article");
    item.className = "test-item";
    item.innerHTML = `
      <h3>${result.label}<span class="badge ${result.passed ? "green" : "red"}">${result.passed ? "GREEN" : "RED"}</span></h3>
      <p>${result.detail} / ${impactSummary(result)}</p>
    `;
    listEl.appendChild(item);
  });
}

async function patchCase(patch) {
  currentPayload = await api("/api/case", { method: "PATCH", body: JSON.stringify(patch) });
  render(currentPayload);
}

async function patchDocument(documentId, status) {
  currentPayload = await api(`/api/documents/${documentId}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
  render(currentPayload);
}

async function patchHeir(heirId, patch) {
  currentPayload = await api(`/api/heirs/${heirId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
  render(currentPayload);
}

function relationshipForHeir(heir) {
  return HEIR_RELATIONSHIP_BY_NAME[heir.name] || (heir.relation === "spouse" ? "spouse" : "eldest_son");
}

function beginHeirEdit(heir) {
  editingHeirId = heir.id;
  qs("#heirRelationshipSelect").value = relationshipForHeir(heir);
  qs("#heirResidenceSelect").value = heir.co_resident ? "co_resident" : "separate";
  qs("#heirFormTitle").textContent = `${heir.name}を修正`;
  qs("#heirSubmitButton").textContent = "更新";
  qs("#heirEditCancelButton").hidden = false;
  setHeirError("");
  qs("#heirRelationshipSelect").focus();
}

function cancelHeirEdit() {
  editingHeirId = null;
  qs("#heirFormTitle").textContent = "相続人登録";
  qs("#heirSubmitButton").textContent = "追加";
  qs("#heirEditCancelButton").hidden = true;
  qs("#heirRelationshipSelect").value = "spouse";
  qs("#heirResidenceSelect").value = "co_resident";
  setHeirError("");
}

async function submitHeir() {
  const relationship = qs("#heirRelationshipSelect").value;
  const coResident = qs("#heirResidenceSelect").value === "co_resident";
  const heirId = editingHeirId;
  currentPayload = await api(heirId ? `/api/heirs/${heirId}` : "/api/heirs", {
    method: heirId ? "PATCH" : "POST",
    body: JSON.stringify({ relationship, co_resident: coResident }),
  });
  render(currentPayload);
  cancelHeirEdit();
}

async function deleteHeir(heirId) {
  currentPayload = await api(`/api/heirs/${heirId}`, { method: "DELETE" });
  render(currentPayload);
  if (editingHeirId === heirId) cancelHeirEdit();
  setHeirError("");
}

async function saveOverallOpinion(value) {
  currentPayload = await api("/api/manual/overall-opinion", {
    method: "PATCH",
    body: JSON.stringify({ overall_opinion: value }),
  });
  render(currentPayload);
}

let isRunning = false;

function setRunning(running) {
  isRunning = running;
  const status = qs("#runStatus");
  const state = currentPayload && currentPayload.state;
  const awaiting = Boolean(currentPayload && currentPayload.last_run && currentPayload.last_run.status === "AWAITING_CLARIFICATION");
  const cardReady = Boolean(state && Array.isArray(state.heirs) && state.heirs.length > 0 && state.home_acquirer_id);
  qs("#runButton").disabled = running || awaiting;
  qs("#cardReviewButton").disabled = running || awaiting || !cardReady;
  qs("#resumeButton").disabled = running || !awaiting;
  if (running) {
    status.hidden = false;
    status.textContent = "Geminiが相談文を確認中…（エージェント実行中）";
  } else {
    status.hidden = true;
    status.textContent = "";
    qs("#runButton").disabled = awaiting;
  }
  if (running) {
    renderExecutionLock(true);
  } else if (currentPayload) {
    render(currentPayload);
  }
}

async function runConsultation() {
  if (isRunning) return;
  const text = qs("#consultationText").value.trim();
  if (!text) {
    setRunError("相談文は8文字以上で入力してください。");
    return;
  }
  setRunError("");
  setRunning(true);
  try {
    const result = await api("/api/run", { method: "POST", body: JSON.stringify({ text }) });
    currentPayload = result.case;
    render(currentPayload);
    setRunError("");
  } finally {
    setRunning(false);
  }
}

async function resumeConsultation() {
  if (isRunning) return;
  const answer = qs("#clarificationAnswer").value.trim();
  if (answer.length < 2) {
    setClarificationError("追加回答は2文字以上で入力してください。");
    return;
  }
  setClarificationError("");
  setRunning(true);
  try {
    const result = await api("/api/run/continue", {
      method: "POST",
      body: JSON.stringify({ answer }),
    });
    currentPayload = result.case;
    render(currentPayload);
  } finally {
    setRunning(false);
  }
}

async function loadAmbiguousDemo() {
  if (isRunning) return;
  await api("/api/demo/seed", { method: "POST" });
  const result = await api("/api/demo/clear-heirs", { method: "POST" });
  currentPayload = result.case;
  qs("#consultationText").value = "父が亡くなり、次男が実家を引き継ぐ話になっています。必要な確認を進めてください。";
  render(currentPayload);
  qs("#consultationText").focus();
}

async function runCardReview() {
  if (isRunning) return;
  setRunError("");
  setRunning(true);
  try {
    const result = await api("/api/review/from-cards", { method: "POST" });
    currentPayload = result.case;
    render(currentPayload);
    setRunError("");
  } finally {
    setRunning(false);
  }
}

async function approveWord() {
  setRunError("");
  await api("/api/approve", { method: "POST" });
  await refresh();
}

function apiErrorMessage(status, body) {
  const detail = body && body.detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const message = detail[0].msg || detail[0].message || "";
    return message.replace(/^Value error,\s*/, "") || "入力内容を確認してください。";
  }
  if (detail === "approval_required") {
    return "レビュー完了（承認）後にWord出力できます。";
  }
  if (detail === "review_not_ready") {
    return "カード内容でReview作成、または相談文を実行してからレビュー完了（承認）してください。";
  }
  if (detail === "heirs_required_for_review") {
    return "相続人カードを1人以上登録してからReview作成してください。";
  }
  if (detail === "home_acquirer_required_for_review") {
    return "自宅取得者を選択してからReview作成してください。";
  }
  if (detail === "run_in_progress") {
    return "処理中です。完了するまでお待ちください。";
  }
  if (detail === "run_cooldown") {
    return "連続実行を防止しています。少し待ってから再実行してください。";
  }
  if (detail === "run_limit_exceeded") {
    return "このセッションの実行上限に達しました。新しいセッションでお試しください。";
  }
  if (detail === "clarification_not_pending") {
    return "追加情報待ちの相談がありません。もう一度相談文を実行してください。";
  }
  if (detail === "clarification_pending") {
    return "追加情報待ちです。質問へ回答して再開するか、デモを初期化してください。";
  }
  if (typeof detail === "string" && detail) {
    return detail;
  }
  if (status === 422) {
    return "入力内容を確認してください。";
  }
  return `処理に失敗しました（${status}）`;
}

function setRunError(message) {
  const error = qs("#runError");
  if (!message) {
    error.textContent = "";
    error.hidden = true;
    return;
  }
  error.textContent = message;
  error.hidden = false;
}

function setHeirError(message) {
  const error = qs("#heirError");
  if (!message) {
    error.textContent = "";
    error.hidden = true;
    return;
  }
  error.textContent = message;
  error.hidden = false;
}

function setClarificationError(message) {
  const error = qs("#clarificationError");
  if (!message) {
    error.textContent = "";
    error.hidden = true;
    return;
  }
  error.textContent = message;
  error.hidden = false;
}

function list(element, items) {
  element.innerHTML = "";
  items.forEach((text) => {
    const li = document.createElement("li");
    li.textContent = text;
    element.appendChild(li);
  });
}

function yen(value) {
  return `${Number(value || 0).toLocaleString("ja-JP")}円`;
}

function taxablePriceImpact(value) {
  return `課税価格 +${yen(value)}`;
}

function actionSummary(action) {
  if (action.type === "alert_small_residence_ineligible") {
    return `${action.type}: ${action.value}`;
  }
  if (action.type === "ask_secondary_inheritance_review") {
    return `${action.type}: ${action.value}`;
  }
  if (action.type === "populate_heir_cards") {
    return `${action.type}: ${action.value.join(" / ")}`;
  }
  const value = Array.isArray(action.labels)
    ? action.labels.slice(0, 3).join(" / ")
    : Array.isArray(action.value)
      ? action.value.slice(0, 3).join(" / ")
      : action.value || "";
  return `${action.type}: ${value}`;
}

function impactSummary(result) {
  if (!result.monetary) {
    return result.impact_label || "○×確認";
  }
  return `${result.impact_label}: ${yen(result.potential_damage_yen)}`;
}

function qs(selector) {
  return document.querySelector(selector);
}

qs("#partitionSelect").addEventListener("change", (event) => {
  patchCase({ partition_status: event.target.value }).catch((error) => {
    setRunError(error.message);
  });
});

qs("#seedButton").addEventListener("click", async () => {
  await api("/api/demo/seed", { method: "POST" });
  await refresh();
});

qs("#heirRegistrationForm").addEventListener("submit", (event) => {
  event.preventDefault();
  submitHeir().catch((error) => {
    setHeirError(error.message);
  });
});

qs("#heirEditCancelButton").addEventListener("click", cancelHeirEdit);

qs("#cardReviewButton").addEventListener("click", () => {
  runCardReview().catch((error) => {
    setRunError(error.message);
  });
});

qs("#runButton").addEventListener("click", () => {
  runConsultation().catch((error) => {
    setRunError(error.message);
  });
});

qs("#resumeButton").addEventListener("click", () => {
  resumeConsultation().catch((error) => {
    setClarificationError(error.message);
  });
});

qs("#ambiguousDemoButton").addEventListener("click", () => {
  loadAmbiguousDemo().catch((error) => {
    setRunError(error.message);
  });
});

qs("#approveButton").addEventListener("click", () => {
  approveWord().catch((error) => {
    setRunError(error.message);
  });
});

qs("#inlineApproveButton").addEventListener("click", () => {
  approveWord().catch((error) => {
    setRunError(error.message);
  });
});

[qs("#wordLink"), qs("#inlineWordLink")].forEach((link) => {
  link.addEventListener("click", (event) => {
    if (link.getAttribute("aria-disabled") === "true" || isRunning) {
      event.preventDefault();
    }
  });
});

qs("#saveOverallOpinionButton").addEventListener("click", () => {
  const value = qs("#overallOpinionInput").value;
  saveOverallOpinion(value).catch((error) => {
    setRunError(error.message);
  });
});

qs("#clearOverallOpinionButton").addEventListener("click", () => {
  saveOverallOpinion("").catch((error) => {
    setRunError(error.message);
  });
});

refresh().catch((error) => {
  document.body.innerHTML = `<main class="shell"><h1>読み込みエラー</h1><p>${error.message}</p></main>`;
});
