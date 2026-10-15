    async function initializePage() {
        await loadXpathSettings();
        await loadCaptureSettings();
        await loadCapturePresets();
        await loadReplySettings();
        let previousLogs = [];
        try {
            const state = await api("/api/status");
            previousLogs = state.logs || [];
            restoreBatch(state.batch);
            hideInitialFinishedStatus = ["succeeded", "stopped", "failed"].includes(state.status)
        } catch {
        }
        oldLogMarker = previousLogs.length ? previousLogs[previousLogs.length - 1] : null;
        try {
            const initialized = await api("/api/page/initialize", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: "{}"
            });
            oldLogMarker = null;
            hideOldLogs = false;
            hideInitialFinishedStatus = false;
            restoreBatch(initialized.batch);
            const run = initialized.run;
            if (run?.status === "running") {
                if (!initialized.batch || !["running", "stopping"].includes(initialized.batch.status)) setVideoLinks([run.source_url || ""]);
            }
        } catch {
            try {
                await api("/api/logs/clear", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: "{}"
                })
            } catch {
            }
            hideOldLogs = Boolean(oldLogMarker)
        }
        $("log").textContent = "暂无运行日志";
        centerSelectArrows();
        poll();
        pollReplyQueue();
        pollBulkChat();
        loadRuns()
    }

    /** 页面打开时读取本机确认状态；首次确认后，刷新及重启均不重复提示。 */
    async function initializeWithDisclaimer() {
        try {
            const status = await api('/api/disclaimer/status');
            if (status.accepted) {
                $('disclaimerDialog').classList.remove('show');
                $('disclaimerDialog').setAttribute('aria-hidden', 'true');
                $('workbench').removeAttribute('inert');
                await initializePage();
                return;
            }
        } catch (error) {
            $('disclaimerError').textContent = error.message;
            $('disclaimerError').hidden = false;
        }
        $('disclaimerCheckbox').focus();
    }

    /** 只有勾选并经服务端持久化确认，才解锁工作台及业务接口。 */
    async function confirmDisclaimer() {
        if (!$('disclaimerCheckbox').checked) return;
        const button = $('confirmDisclaimer');
        button.disabled = true;
        $('disclaimerError').hidden = true;
        try {
            await api('/api/disclaimer/accept', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({accepted: true})
            });
            $('disclaimerDialog').classList.remove('show');
            $('disclaimerDialog').setAttribute('aria-hidden', 'true');
            $('workbench').removeAttribute('inert');
            await initializePage();
        } catch (error) {
            $('disclaimerError').textContent = error.message;
            $('disclaimerError').hidden = false;
            button.disabled = !$('disclaimerCheckbox').checked;
        }
    }

    $('disclaimerCheckbox').onchange = () => {
        $('confirmDisclaimer').disabled = !$('disclaimerCheckbox').checked
    };
    $('confirmDisclaimer').onclick = confirmDisclaimer;
    initializeWithDisclaimer();
