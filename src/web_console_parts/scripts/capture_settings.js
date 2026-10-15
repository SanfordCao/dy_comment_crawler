    const payload = () => ({
        url: $("url").value.trim(),
        max_comments: +$("maxComments").value,
        max_minutes: +$("maxMinutes").value,
        min_delay_ms: +$("minDelay").value,
        max_delay_ms: +$("maxDelay").value,
        scroll_direction: $("direction").value,
        comment_scope: $("commentScope").value,
        fetch_mode: $("fetchMode").value,
        fetch_douyin_id: $("fetchDouyinId").checked,
        concurrent: $("concurrent").checked,
        concurrency_limit: +$("concurrencyLimit").value,
        challenge_wait_minutes: +$("waitMinutes").value,
        idle_rounds: 8,
        scroll_pixels: 700
    });

    const captureFields = {
        comment_scope: "commentScope", fetch_mode: "fetchMode",
        max_comments: "maxComments", max_minutes: "maxMinutes",
        scroll_direction: "direction", min_delay_ms: "minDelay",
        max_delay_ms: "maxDelay", challenge_wait_minutes: "waitMinutes",
        fetch_douyin_id: "fetchDouyinId", concurrent: "concurrent",
        concurrency_limit: "concurrencyLimit"
    };

    // 并发关闭时隐藏数量输入，保留已设置的值供下次开启使用。
    function syncConcurrencyOptions() {
        $("concurrencyOptions").hidden = !$("concurrent").checked;
    }

    $("concurrent").addEventListener("change", syncConcurrencyOptions);

    /** 将 settings 回显到表单，无返回值；只显示参数，不触发保存或方案切换。 */
    function applyCaptureSettings(settings) {
        for (const [key, id] of Object.entries(captureFields)) {
            if (settings[key] == null) continue;
            if ($(id).type === "checkbox") $(id).checked = settings[key] === true;
            else $(id).value = settings[key];
        }
        syncConcurrencyOptions();
        centerSelectArrows();
    }

    /** 读取当前实际采集参数并缓存，预览其他方案时仍以缓存作为启动参数。 */
    async function loadCaptureSettings() {
        try {
            const settings = await api("/api/capture-settings");
            activeCaptureSettings = {...settings};
            applyCaptureSettings(settings);
        } catch (error) {
            showNotice(`读取采集配置失败：${error.message}`, true);
        }
    }

    let captureSaveChain = Promise.resolve();
    /** 返回表单参数副本；仅用于方案编辑，不能直接作为实际采集配置。 */
    const currentCaptureSettings = () => {
        const options = payload();
        return Object.fromEntries(Object.keys(captureFields).map(key => [key, options[key]]));
    };
    Object.values(captureFields).forEach(id => $(id).addEventListener("change", () => {
        // 所有编辑都先保留为草稿，点击保存才持久化，取消不会污染使用中配置。
        updatePresetHint();
    }));

    let presetData = {selected: "", presets: []}, presetBusy = false, captureRunning = false;
    let shareValidationPending = false;
    let presetDeleteTarget = null;
    let viewedPreset = "", presetEditing = false, presetCreating = false, activeCaptureSettings = {};

    /** 显示查看/编辑提示，无参数、无返回值，强调预览不等于正在使用。 */
    function updatePresetHint() {
        $("presetHint").textContent = `当前使用：${presetData.selected || "默认配置"}。查看其他方案不会切换采集配置。`;
        $("presetDetailTitle").textContent = presetCreating ? "新增方案" : `${presetEditing ? "编辑" : "查看"}：${viewedPreset || "默认配置"}`;
    }

    /** 同步表格、编辑控件禁用状态；预览始终只读，运行中禁止修改方案。 */
    function syncPresetControls() {
        const disabled = presetBusy || captureRunning;
        $("presetManager").querySelectorAll("input,select,button").forEach(el => {
            el.disabled = el.dataset.presetView === "true" ? presetBusy : disabled;
            if (el.dataset.permanentlyDisabled === "true") el.disabled = true;
        });
        $("updatePreset").hidden = !presetEditing || presetCreating;
        $("savePreset").hidden = !presetCreating;
        $("savePreset").disabled = disabled;
        $("cancelPresetEdit").hidden = !presetEditing;
        $("cancelPresetEdit").textContent = presetCreating ? "取消" : "取消编辑";
        $("updatePreset").disabled = disabled;
        $("cancelPresetEdit").disabled = disabled;
        $("confirmDeletePreset").disabled = disabled;
        $("cancelDeletePreset").disabled = presetBusy;
        $("presetName").disabled = disabled;
        $("confirmPresetName").disabled = disabled;
        $("cancelPresetName").disabled = presetBusy;
        // 加载方案时短暂锁住表单与启动按钮，避免新旧设置交错提交。
        Object.values(captureFields).forEach(id => {
            $(id).disabled = disabled || !presetEditing;
        });
        $("start").disabled = disabled;
    }

    /** 查看或编辑 name 对应方案；edit 为真才允许修改，不切换当前使用方案。 */
    function viewCapturePreset(name, edit = false) {
        if (presetBusy || (edit && captureRunning)) return;
        const preset = presetData.presets.find(item => item.name === name);
        if (name ? !preset : presetData.default_enabled === false) return;
        viewedPreset = name;
        presetCreating = false;
        presetEditing = edit;
        // 查看正在使用的方案时显示实际参数，而非可能尚未更新的旧快照。
        const settings = name === presetData.selected || !name ? activeCaptureSettings : preset.settings;
        applyCaptureSettings(settings);
        renderPresetRows();
        updatePresetHint();
        syncPresetControls();
    }

    /** 判断方案是否可删除；当前使用方案和唯一剩余方案始终保留。 */
    function canDeletePreset(name) {
        const hasDefault = presetData.default_enabled !== false;
        const exists = name ? presetData.presets.some(item => item.name === name) : hasDefault;
        return exists && name !== presetData.selected && presetData.presets.length + Number(hasDefault) > 1;
    }

    /** 构建方案表格，名称通过 textContent 显示；已删除的默认方案不再生成。 */
    function renderPresetRows() {
        const body = $("presetRows");
        body.replaceChildren();
        for (const preset of [...(presetData.default_enabled === false ? [] : [{name: ""}]), ...presetData.presets]) {
            const row = document.createElement("tr"), name = document.createElement("td"),
                state = document.createElement("td"), operations = document.createElement("td");
            row.className = [preset.name === presetData.selected ? "preset-current" : "", preset.name === viewedPreset ? "preset-viewed" : ""].join(" ");
            name.textContent = preset.name || "默认配置";
            const isCurrent = preset.name === presetData.selected;
            const badge = document.createElement("span");
            badge.className = "preset-badge" + (isCurrent ? "" : " preset-badge-inactive");
            badge.textContent = isCurrent ? "当前使用" : "未使用";
            state.append(badge);
            const actions = document.createElement("div");
            actions.className = "preset-row-actions";
            for (const [label, action] of [["查看", () => viewCapturePreset(preset.name)], ["编辑", () => viewCapturePreset(preset.name, true)], ["应用", () => applyCapturePreset(preset.name)], ["删除", () => openPresetDeleteDialog(preset.name)]]) {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "btn" + (label === "删除" ? " danger" : "");
                button.textContent = label;
                button.disabled = presetBusy || (label !== "查看" && captureRunning) || (label === "删除" && !canDeletePreset(preset.name));
                if (label !== "查看") button.dataset.presetMutation = "true";
                else button.dataset.presetView = "true";
                if (label === "删除" && !canDeletePreset(preset.name)) {
                    button.dataset.permanentlyDisabled = "true";
                    button.title = preset.name === presetData.selected ? "当前使用的配置不能删除" : "至少保留一个配置方案";
                }
                // 正在使用的方案无需重复应用，轮询刷新时也保持按钮禁用。
                if (label === "应用" && preset.name === presetData.selected) {
                    button.dataset.permanentlyDisabled = "true";
                    button.disabled = true;
                }
                button.onclick = action;
                actions.append(button);
            }
            operations.append(actions);
            row.append(name, state, operations);
            body.append(row);
        }
        // 新增入口固定在最后一行，不与已保存方案混淆。
        const addRow = document.createElement("tr"), cell = document.createElement("td"),
            add = document.createElement("button");
        cell.colSpan = 3;
        cell.className = "preset-add-cell";
        add.type = "button";
        add.className = "preset-add";
        add.textContent = "+";
        add.setAttribute("aria-label", "新增配置方案");
        add.disabled = presetBusy || captureRunning;
        add.onclick = beginCreatePreset;
        cell.append(add);
        addRow.append(cell);
        body.append(addRow);
    }

    /** 开始新增草稿；使用服务端默认值，不继承当前方案，也不写入持久化配置。 */
    function beginCreatePreset() {
        if (presetBusy || captureRunning || !presetData.defaults) return;
        presetCreating = true;
        presetEditing = true;
        viewedPreset = "";
        applyCaptureSettings(presetData.defaults);
        updatePresetHint();
        syncPresetControls();
    }

    /** 接收服务端方案列表，重绘表格；默认下方展示当前使用配置。 */
    function renderCapturePresets(data) {
        presetData = data;
        closePresetDeleteDialog();
        renderPresetRows();
        updatePresetHint();
        syncPresetControls();
        centerSelectArrows();
    }

    /** 读取保存方案并默认查看当前使用的配置，不启动采集。 */
    async function loadCapturePresets() {
        try {
            renderCapturePresets(await api("/api/capture-presets"));
            viewCapturePreset(presetData.selected);
        } catch (error) {
            showNotice(`读取配置方案失败：${error.message}`, true);
        }
    }

    /** 应用表格中 name 对应的方案；默认配置沿用实际参数，不读取其他方案预览。 */
    function applyCapturePreset(name) {
        return managePreset(name ? "select" : "custom", name, name ? null : activeCaptureSettings);
    }

    /** 执行 action/name 操作；settingsOverride 可指定参数，无值时使用编辑表单。 */
    async function managePreset(action, name, settingsOverride = null) {
        if (presetBusy || captureRunning) return;
        presetBusy = true;
        syncPresetControls();
        // 排在当前表单的自动保存之后，防止旧设置晚到覆盖新加载的方案。
        const settings = settingsOverride || currentCaptureSettings();
        const request = captureSaveChain.then(() => api("/api/capture-presets", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({action, name, settings})
        }));
        captureSaveChain = request.catch(() => {
        });
        try {
            const data = await request;
            activeCaptureSettings = {...data.settings};
            const keepView = ["create", "update"].includes(action) ? name : data.selected;
            renderCapturePresets(data);
            presetBusy = false;
            viewCapturePreset(keepView);
            if (action === "create") $("presetName").value = "";
            showNotice({
                create: "配置方案已保存",
                update: "当前方案已更新",
                select: "配置方案已应用",
                delete: "配置方案已删除，当前设置保留",
                custom: "默认配置已保存并应用"
            }[action]);
            return true;
        } catch (error) {
            showNotice(error.message, true);
            return false;
        } finally {
            presetBusy = false;
            syncPresetControls();
        }
    }

    /** 关闭命名弹窗但保留新增草稿，取消后可继续调整参数。 */
    function closePresetNameDialog() {
        $("presetNameDialog").classList.remove("show");
        $("presetNameDialog").setAttribute("aria-hidden", "true");
    }

    /** 打开命名弹窗，无副作用；只有确认保存才发出新建请求。 */
    function openPresetNameDialog() {
        if (!presetCreating || presetBusy || captureRunning) return;
        $("presetName").value = "";
        $("presetNameError").hidden = true;
        $("presetNameDialog").classList.add("show");
        $("presetNameDialog").setAttribute("aria-hidden", "false");
        $("presetName").focus();
    }

    /** 校验名称并保存草稿；失败保留弹窗和参数，避免重复输入。 */
    async function confirmPresetName() {
        if (presetBusy || captureRunning) return;
        const name = $("presetName").value.trim();
        if (!name || name.length > 50 || presetData.presets.some(item => item.name === name)) {
            $("presetNameError").textContent = !name ? "请输入配置方案名称" : name.length > 50 ? "名称不能超过 50 个字符" : "同名方案已存在，请更换名称";
            $("presetNameError").hidden = false;
            return;
        }
        if (await managePreset("create", name)) closePresetNameDialog();
    }

    $("savePreset").onclick = openPresetNameDialog;
    $("confirmPresetName").onclick = confirmPresetName;
    $("cancelPresetName").onclick = () => {
        if (!presetBusy) closePresetNameDialog();
    };
    $("presetNameDialog").onclick = event => {
        if (event.target === $("presetNameDialog") && !presetBusy) closePresetNameDialog();
    };
    $("presetName").addEventListener("keydown", event => {
        if (event.key === "Enter") {
            event.preventDefault();
            confirmPresetName();
        }
    });
    $("updatePreset").onclick = () => {
        if (viewedPreset) managePreset("update", viewedPreset);
        else managePreset("custom", "");
    };
    $("cancelPresetEdit").onclick = () => viewCapturePreset(presetCreating ? presetData.selected : viewedPreset);

    /** 关闭方案删除弹窗，清空固定目标，不更改任何配置。 */
    function closePresetDeleteDialog() {
        const dialog = $("presetDeleteDialog");
        const wasOpen = dialog.classList.contains("show");
        dialog.classList.remove("show");
        dialog.setAttribute("aria-hidden", "true");
        presetDeleteTarget = null;
        if (wasOpen) $("presetDetailTitle").focus();
    }

    /** 打开 name 对应的删除确认弹窗；确认目标固定，防止查看切换后误删。 */
    function openPresetDeleteDialog(name) {
        if (presetBusy || captureRunning || !canDeletePreset(name)) return;
        // 固定弹窗打开时的方案名称，确认只会删除用户看到的这一套方案。
        presetDeleteTarget = name;
        $("presetDeleteName").textContent = presetDeleteTarget || "默认配置";
        $("presetDeleteDialog").classList.add("show");
        $("presetDeleteDialog").setAttribute("aria-hidden", "false");
        $("cancelDeletePreset").focus();
    }

    $("confirmDeletePreset").onclick = () => {
        // 空字符串是有效的默认方案标识，不能用真假判断忽略它。
        if (presetDeleteTarget !== null && canDeletePreset(presetDeleteTarget)) managePreset("delete", presetDeleteTarget);
    };
    $("cancelDeletePreset").onclick = () => {
        if (!presetBusy) closePresetDeleteDialog();
    };
    $("presetDeleteDialog").onclick = event => {
        if (event.target === $("presetDeleteDialog") && !presetBusy) closePresetDeleteDialog();
    };

