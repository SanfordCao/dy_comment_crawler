    const videoUrls = () => Array.from($("videoLinks").querySelectorAll("input"), input => input.value.trim()).filter(Boolean);

    /** 清空后输入的重复项；启动前兜底扫描时保留每个视频的首行。 */
    function checkDuplicateShareTexts(changedInput = null) {
        const inputs = Array.from($("videoLinks").querySelectorAll("input")), duplicates = [], seen = new Map();
        for (const [index, input] of inputs.entries()) {
            if (changedInput && input !== changedInput) continue;
            const key = shareTextKey(input.value);
            if (!key) continue;
            const previous = changedInput ? inputs.findIndex(other => other !== input && shareTextKey(other.value) === key) : seen.get(key);
            if (previous !== undefined && previous !== -1) {
                duplicates.push(`第 ${index + 1} 条与第 ${previous + 1} 条重复`);
                input.value = "";
                input.removeAttribute("aria-invalid");
            } else seen.set(key, index);
        }
        if (duplicates.length) showNotice(`${duplicates.join("；")}，已清空重复输入框`, true);
        return duplicates.length > 0;
    }

    /** 按行数同步操作：单行仅加号，多行仅末行保留加号，所有行可删除。 */
    function syncVideoLinkButtons() {
        const rows = Array.from($("videoLinks").children);
        rows.forEach((row, index) => {
            const last = index === rows.length - 1;
            row.querySelector(".video-link-remove").hidden = rows.length === 1;
            row.querySelector(".video-link-add:not(.video-link-remove)").hidden = !last;
            row.classList.toggle("removable", rows.length > 1 && last);
            // 首行删除后仍将标签关联到新的首个输入框。
            const input = row.querySelector("input");
            if (index === 0) input.id = "url"; else input.removeAttribute("id");
            row.querySelectorAll("input,button").forEach(el => el.disabled = captureRunning);
        });
    }

    /** 新增链接行并刷新全部操作按钮；返回新输入框供调用方聚焦。 */
    function addVideoLink(value = "", after = null) {
        const box = $("videoLinks");
        if (box.children.length >= 100) {
            showNotice("最多添加 100 个视频链接", true);
            return
        }
        const row = document.createElement("div"), input = document.createElement("input"),
            add = document.createElement("button");
        row.className = "video-link-row";
        // 分享文字不是 URL，使用文本输入让后端统一提取和校验，避免浏览器提前拒绝。
        input.type = "text";
        input.placeholder = "请粘贴抖音完整分享文本";
        input.maxLength = 16384;
        input.autocomplete = "off";
        input.value = value;
        input.oninput = () => {
            input.removeAttribute("aria-invalid");
            checkDuplicateShareTexts(input);
        };
        input.setAttribute("aria-label", "抖音视频链接");
        if (!box.children.length) input.id = "url";
        add.type = "button";
        add.className = "video-link-add";
        add.textContent = "+";
        add.setAttribute("aria-label", "新增视频链接");
        add.title = "新增一行视频链接";
        add.onclick = () => {
            const next = addVideoLink("", row);
            next?.focus()
        };
        row.append(input, add);
        {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "video-link-add video-link-remove";
            remove.textContent = "−";
            remove.setAttribute("aria-label", "删除此视频链接");
            remove.title = "删除此行视频链接";
            remove.onclick = () => {
                const neighbor = row.previousElementSibling || row.nextElementSibling;
                row.remove();
                syncVideoLinkButtons();
                neighbor?.querySelector("input")?.focus();
            };
            row.insertBefore(remove, add);
        }
        if (after) after.after(row); else box.append(row);
        syncVideoLinkButtons();
        return input
    }

    /** 用 urls 重建链接列表，空数组至少保留一个输入框。 */
    function setVideoLinks(urls) {
        $("videoLinks").replaceChildren();
        (urls.length ? urls : [""]).forEach(url => addVideoLink(url))
    }

    setVideoLinks([""]);

    function restoreBatch(batch) {
        renderBatch(batch);
        if (!batch || !["running", "stopping"].includes(batch.status)) return;
        setVideoLinks(batch.items.filter(item => ["pending", "running", "waiting_for_user", "stopping"].includes(item.status)).map(item => item.share_text || item.url));
    }

    /** 按原输入行号显示所有错误，外部分享内容仅作为纯文本插入。 */
    function showShareValidationDialog(issues) {
        const list = $("shareValidationIssues"), inputs = Array.from($("videoLinks").querySelectorAll("input"));
        list.replaceChildren();
        inputs.forEach(input => input.removeAttribute("aria-invalid"));
        for (const issue of issues || []) {
            const entry = document.createElement("p");
            entry.style.whiteSpace = "pre-line";
            entry.textContent = `第 ${issue.row} 条：${issue.reason}${issue.text ? "\n" + issue.text : ""}`;
            list.append(entry);
            inputs[issue.row - 1]?.setAttribute("aria-invalid", "true");
            // 不同短链需后台解析后才能确认重复，只清空服务器明确标记的重复行。
            if (issue.clear_input && inputs[issue.row - 1]) inputs[issue.row - 1].value = "";
        }
        $("shareValidationDialog").classList.add("show");
        $("shareValidationDialog").setAttribute("aria-hidden", "false");
        $("closeShareValidationDialog").focus();
    }

    /** 关闭提示后定位第一条无效输入，保留全部已粘贴内容供修改。 */
    function closeShareValidationDialog() {
        $("shareValidationDialog").classList.remove("show");
        $("shareValidationDialog").setAttribute("aria-hidden", "true");
        $("videoLinks").querySelector('input[aria-invalid="true"]')?.focus();
    }

    $("closeShareValidationDialog").onclick = closeShareValidationDialog;
    $("shareValidationDialog").onclick = event => {
        if (event.target === $("shareValidationDialog")) closeShareValidationDialog();
    };

    $("start").onclick = async () => {
        if (licenseFailure) {
            showLicenseFailure(licenseFailure);
            return;
        }
        const button = $("start");
        if (shareValidationPending) return;
        if (checkDuplicateShareTexts()) return;
        shareValidationPending = true;
        button.disabled = true;
        button.textContent = "正在校验分享文本…";
        $("videoLinks").querySelectorAll("input,button").forEach(el => el.disabled = true);
        try {
            await captureSaveChain;
            // 启动只使用已保存且正在使用的参数，绝不读取其他方案的预览/草稿。
            const options = {...payload(), ...activeCaptureSettings};
            // 不过滤空行，以便服务端返回的行号与实际输入框保持一致。
            options.share_texts = Array.from($("videoLinks").querySelectorAll("input"), input => input.value.trim());
            delete options.url;
            delete options.urls;
            await api("/api/crawl/start", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(options)
            });
            captureRunning = true;
            showNotice(options.concurrent ? `任务已启动，最多同时采集 ${options.concurrency_limit} 个视频` : "任务已启动，视频将按顺序采集")
        } catch (error) {
            if (error.code === "share_validation_failed") showShareValidationDialog(error.issues);
            else if (!error.licenseFailure) showNotice(error.message, true);
        } finally {
            shareValidationPending = false;
            button.textContent = "启动采集任务";
            button.disabled = Boolean(licenseFailure) || captureRunning;
            $("videoLinks").querySelectorAll("input,button").forEach(el => el.disabled = captureRunning);
        }
    };

    /** 显示本轮全部任务及最终状态；下一次成功启动才由服务端替换本轮队列。 */
    function renderBatch(batch) {
        const isQueued = item => ["pending", "running", "waiting_for_user", "stopping"].includes(item.status);
        // 已结束任务仍保留在服务端内存，不再过滤，方便查看整轮结果。
        const items = (batch?.items || []).map(item => ({...item, batchId: batch.id}));
        for (const previous of batch?.unfinished_batches || []) {
            items.push(...previous.items.filter(isQueued).map(item => ({...item, batchId: previous.id})));
        }
        const visible = items.length > 0;
        $("batchProgress").hidden = !visible;
        if (!visible) {
            $("batchItems").replaceChildren();
            $("batchSummary").textContent = "";
            return;
        }
        const labels = {
            pending: "等待",
            running: "采集中",
            waiting_for_user: "等待人工处理",
            stopping: "正在停止",
            succeeded: "完成",
            failed: "失败",
            stopped: "已停止",
            cancelled: "未启动",
            interrupted: "已中断"
        };
        const remaining = items.filter(isQueued).length;
        $("batchSummary").textContent = `共 ${items.length} 个 · 已结束 ${items.length - remaining} 个 · 未完成 ${remaining} 个`;
        const box = $("batchItems"), scroll = box.scrollTop;
        box.replaceChildren();
        items.forEach((item, index) => {
            const row = document.createElement("div");
            row.className = `batch-item ${item.status}`;
            const head = document.createElement("div");
            head.className = "batch-item-head";
            const title = document.createElement("strong"), state = document.createElement("span");
            title.textContent = `视频 ${index + 1}`;
            state.textContent = labels[item.status] || item.status;
            const actions = document.createElement("div"), stop = document.createElement("button");
            actions.className = "queue-actions";
            stop.className = "icon-button queue-stop";
            stop.type = "button";
            stop.title = item.status === "pending" ? "停止此任务，不再采集" : "停止此视频采集";
            stop.setAttribute("aria-label", `停止视频 ${index + 1}`);
            stop.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>';
            stop.disabled = item.batchId !== batch.id || batch.status !== "running" || !["pending", "running", "waiting_for_user"].includes(item.status);
            // 终态任务仅展示进度，不再提供无效的停止操作。
            stop.hidden = !isQueued(item);
            stop.onclick = async () => {
                stop.disabled = true;
                try {
                    await api("/api/crawl/stop-task", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({batch_id: item.batchId, url: item.url})
                    });
                    showNotice(item.status === "pending" ? "已停止此任务，将跳过该视频" : "正在停止此视频，保存已有数据后继续下一个");
                } catch (error) {
                    stop.disabled = false;
                    showNotice(error.message, true);
                }
            };
            actions.append(state, stop);
            head.append(title, actions);
            const url = document.createElement("span");
            url.className = "batch-item-url";
            url.textContent = item.url;
            url.title = item.url;
            row.append(head, url);
            if (item.error) {
                const error = document.createElement("div");
                error.className = "batch-item-error";
                error.textContent = item.error;
                row.append(error)
            }
            box.append(row)
        });
        box.scrollTop = scroll
    }

    $("stop").onclick = async () => {
        setStoppingDialog(true);
        try {
            await api("/api/crawl/stop", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})
        } catch (error) {
            setStoppingDialog(false);
            showNotice(error.message, true)
        }
    };
    $("continue").onclick = async () => {
        try {
            await api("/api/crawl/continue", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: "{}"
            })
        } catch (error) {
            showNotice(error.message, true)
        }
    };

    function setStoppingDialog(show) {
        $("stoppingDialog").classList.toggle("show", show);
        $("stoppingDialog").setAttribute("aria-hidden", String(!show))
    }

    let oldLogMarker = null;
    let hideOldLogs = false;
    let hideInitialFinishedStatus = false;

    function visibleLogs(logs) {
        if (!hideOldLogs || !oldLogMarker) return logs;
        const boundary = logs.lastIndexOf(oldLogMarker);
        if (boundary >= 0) return logs.slice(boundary + 1);
        hideOldLogs = false;
        return logs
    }

    /** 更新一个日志框，阅读旧日志时保持位置，位于底部时才跟随新增日志。 */
    function updateLogBox(box, logs, placeholder = "暂无运行日志") {
        const text = logs.length ? logs.join("\n") : placeholder;
        if (box.textContent === text) return;
        // Check before adding new lines, so an existing reader position is kept.
        const followLatest = box.scrollHeight - box.clientHeight - box.scrollTop <= 24;
        const previousTop = box.scrollTop;
        box.textContent = text;
        box.scrollTop = followLatest ? box.scrollHeight : previousTop;
    }

    const taskLogCards = new Map();

    /** 按稳定任务键更新日志卡片，保留每个任务自己的滚动位置。 */
    function renderTaskLogs(tasks, logs) {
        const keys = new Set(tasks.map(task => task.key));
        for (const [key, card] of taskLogCards) {
            if (!keys.has(key)) {
                card.root.remove();
                taskLogCards.delete(key);
            }
        }
        const labels = {
            pending: "等待采集",
            running: "采集中",
            waiting_for_user: "等待人工处理",
            stopping: "正在停止",
            succeeded: "已完成",
            failed: "失败",
            stopped: "已停止",
            cancelled: "未启动",
            interrupted: "已中断"
        };
        for (const task of tasks) {
            let card = taskLogCards.get(task.key);
            if (!card) {
                const root = document.createElement("section"), header = document.createElement("div"),
                    title = document.createElement("strong"), status = document.createElement("span"),
                    url = document.createElement("div"), body = document.createElement("div");
                root.className = "task-log-card";
                header.className = "task-log-header";
                status.className = "task-log-state";
                url.className = "task-log-url";
                body.className = "task-log-body";
                header.append(title, status);
                root.append(header, url, body);
                $("taskLogs").append(root);
                card = {root, title, status, url, body};
                taskLogCards.set(task.key, card);
            }
            card.title.textContent = `视频 ${task.index}`;
            card.status.textContent = labels[task.status] || task.status;
            card.url.textContent = task.url;
            card.body.dataset.empty = String(!task.logs.length);
            updateLogBox(card.body, task.logs, task.status === "pending" ? "等待任务开始…" : "暂无日志");
        }
        // 调度信息保留单独框，已有独立卡片时不重复显示各视频的混合日志。
        const general = tasks.length ? logs.filter(line => !line.startsWith("[视频 ")) : logs;
        $("log").hidden = !general.length;
        updateLogBox($("log"), general);
        $("taskLogs").hidden = !tasks.length;
        $("logEmpty").hidden = Boolean(tasks.length || general.length);
    }

    async function poll() {
        try {
            const state = await api("/api/status");
            // 其他并行接口已经发现过期时，较早返回的状态不能重新启用启动按钮。
            if (licenseFailure) return;
            renderBatch(state.batch);
            syncRunActions(state.batch);
            const finished = ["succeeded", "stopped", "failed"].includes(state.status);
            if (hideInitialFinishedStatus && finished) {
                state.status = "idle";
                state.message = "等待开始"
            } else if (!finished) hideInitialFinishedStatus = false;
            if (state.status === "stopping") setStoppingDialog(true); else if (finished || state.status === "idle") setStoppingDialog(false);
            $("statusText").textContent = state.message;
            $("dot").className = `dot ${state.status}`;
            $("waiting").classList.toggle("show", state.status === "waiting_for_user");
            const active = ["starting", "running", "waiting_for_user", "stopping"].includes(state.status);
            const validating = shareValidationPending || state.share_validation_running;
            if (validating) $("statusText").textContent = "正在校验全部分享文本，通过后才开始采集";
            captureRunning = active;
            syncPresetControls();
            $("start").disabled = active || presetBusy || validating;
            $("start").textContent = validating ? "正在校验分享文本…" : "启动采集任务";
            $("stop").disabled = !active;
            $("videoLinks").querySelectorAll("input,button").forEach(el => el.disabled = active || validating);
            $("stop").textContent = state.batch?.items?.length > 1 ? "停止整批" : "停止采集任务";
            const logs = visibleLogs(state.logs || []);
            renderTaskLogs(state.task_logs || [], logs);
            pollReplyQueue();
            if (lastStatus !== state.status && ["succeeded", "stopped"].includes(state.status)) {
                showNotice(state.message);
                loadRuns()
            }
            if (lastStatus !== state.status && state.status === "failed") {
                showNotice(state.message, true)
            }
            lastStatus = state.status
        } catch {
        }
        setTimeout(poll, 1200)
    }

