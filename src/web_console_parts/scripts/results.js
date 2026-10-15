    let deleteTarget = null;
    /** 当前结果页只操作下拉框选中的一种任务类型。 */
    const historyType = () => $('historyTaskType').value;
    /** 根据结果类型选择隔离的服务端路由。 */
    const historyApiRoot = () => historyType() === 'reply' ? '/api/reply-runs' : '/api/runs';
    const runPublisher = run => run?.video_publisher_nickname || "作者未知";
    /** 回复任务尚无视频元数据时，用视频 ID 区分任务而非显示空标题。 */
    const runVideoTitle = run => run?.video_title || (run?.type === 'reply' ? `视频 ${String(run?.source_url || '').match(/\d{15,22}/)?.[0] || '未知'}` : "标题未知");

    const checkedRuns = new Set();
    let historyRuns = [], historyBusy = false, exportTargets = [];

    /** 同步多选工具栏，运行中的任务不参与选择，预览与勾选互不影响。 */
    function syncHistorySelection() {
        $('historyTaskType').disabled = historyBusy;
        const eligible = historyRuns.filter(run => !run.is_active);
        for (const name of checkedRuns) {
            if (!eligible.some(run => run.name === name)) checkedRuns.delete(name);
        }
        document.querySelectorAll("#runs .run-check").forEach(input => {
            input.checked = checkedRuns.has(input.dataset.runName);
            input.disabled = historyBusy || Boolean(historyRuns.find(run => run.name === input.dataset.runName)?.is_active);
        });
        $("runSelectionCount").textContent = `已选 ${checkedRuns.size} 项`;
        $("selectAllRuns").checked = eligible.length > 0 && checkedRuns.size === eligible.length;
        $("selectAllRuns").indeterminate = checkedRuns.size > 0 && checkedRuns.size < eligible.length;
        $("selectAllRuns").disabled = historyBusy || !eligible.length;
        for (const id of ["exportMerged", "exportSeparate", "deleteRuns"]) $(id).disabled = historyBusy || !checkedRuns.size;
    }

    /** 根据实时队列同步历史任务可操作状态，防止勾选后任务开始仍允许操作。 */
    function syncRunActions(batch) {
        if (historyType() === 'reply') return;
        const activeNames = new Set((batch?.items || [])
            .filter(item => ["running", "waiting_for_user", "stopping"].includes(item.status))
            .map(item => item.run_name));
        historyRuns.forEach(run => run.is_active = activeNames.has(run.name));
        syncHistorySelection();
    }

    /** 读取历史任务并保留有效勾选；返回是否成功，供手动刷新决定是否提示。 */
    async function loadRuns() {
        try {
            const requestedType = historyType();
            const runs = await api(historyApiRoot());
            if (requestedType !== historyType()) return false;
            historyRuns = runs;
            const box = $("runs");
            box.replaceChildren();
            if (!runs.length) {
                box.innerHTML = `<div class="empty">还没有${historyType() === 'reply' ? '回复' : '采集'}结果</div>`;
                syncHistorySelection();
                return true;
            }
            runs.forEach(run => {
                const button = document.createElement("button");
                button.className = "run" + (run.name === selectedRun ? " active" : "");
                const heading = document.createElement("div");
                heading.className = "run-heading";
                const label = document.createElement("span");
                label.className = "run-label";
                label.textContent = historyType() === 'reply' ? runVideoTitle(run) : `${runPublisher(run)}-${runVideoTitle(run)}`;
                heading.append(label);
                const id = document.createElement("span");
                id.className = "run-id";
                id.textContent = run.name;
                button.append(heading, id);
                button.onclick = () => selectRun(run);
                const row = document.createElement("div");
                row.className = "run-item";
                const check = document.createElement("input");
                check.type = "checkbox";
                check.className = "run-check";
                check.dataset.runName = run.name;
                check.setAttribute("aria-label", `选择 ${label.textContent}`);
                check.title = run.is_active ? "正在采集的任务不可导出或删除" : "选择此任务";
                check.onchange = () => {
                    if (check.checked) checkedRuns.add(run.name); else checkedRuns.delete(run.name);
                    syncHistorySelection();
                };
                row.append(check, button);
                box.append(row)
            });
            syncHistorySelection();
            return true;
        } catch (error) {
            showNotice(error.message, true);
            return false;
        }
    }

    /** 冻结勾选的删除目标并列出名称，确认前不对结果目录做任何修改。 */
    function openDeleteDialog() {
        if (historyBusy) return;
        deleteTarget = historyRuns.filter(run => checkedRuns.has(run.name) && !run.is_active).map(run => ({...run}));
        if (!deleteTarget.length) return;
        $('dialogTitle').textContent = `删除所选 ${deleteTarget.length} 个任务？`;
        const box = $('dialogRunName');
        box.replaceChildren();
        for (const info of deleteTarget) {
            const primary = document.createElement('div'), id = document.createElement('div');
            primary.className = 'dialog-run-primary';
            primary.textContent = historyType() === 'reply' ? runVideoTitle(info) : `${runPublisher(info)}-${runVideoTitle(info)}`;
            id.className = 'dialog-run-id';
            id.textContent = info.name;
            box.append(primary, id);
        }
        $('dialogError').hidden = true;
        $('dialogError').textContent = '';
        $('deleteDialog').classList.add('show');
        $('deleteDialog').setAttribute('aria-hidden', 'false');
        $('cancelDelete').focus()
    }

    /** 关闭删除确认；处理中禁止取消，避免批量删除仍在后台继续。 */
    function closeDeleteDialog() {
        if (historyBusy) return;
        $('deleteDialog').classList.remove('show');
        $('deleteDialog').setAttribute('aria-hidden', 'true');
        deleteTarget = null;
        (document.querySelector('.run.active') || $('refresh')).focus()
    }

    /** 逐项删除已确认目标，报告部分失败；成功项清理选中及评论预览。 */
    async function deleteRun() {
        if (!deleteTarget?.length || historyBusy) return;
        const targets = deleteTarget.slice(), deleted = [], failed = [];
        historyBusy = true;
        syncHistorySelection();
        $('confirmDelete').disabled = true;
        $('cancelDelete').disabled = true;
        try {
            for (const target of targets) {
                try {
                    await api(`${historyApiRoot()}/delete`, {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({name: target.name})
                    });
                    deleted.push(target.name);
                    checkedRuns.delete(target.name);
                } catch (error) {
                    failed.push({run: target, message: error.message});
                }
            }
            if (deleted.includes(selectedRun)) {
                selectedRun = "";
                selectedRunInfo = null;
                allComments = [];
                $('runTitle').textContent = "选择一个结果";
                $('runMeta').textContent = "查看评论并下载 Excel";
                $('comments').innerHTML = '<div class="empty">从左侧选择一次采集结果</div>';
                $('more').hidden = true;
            }
            historyBusy = false;
            if (failed.length) {
                deleteTarget = failed.map(item => item.run);
                const box = $('dialogRunName');
                box.replaceChildren();
                for (const {run} of failed) {
                    const primary = document.createElement('div'), id = document.createElement('div');
                    primary.className = 'dialog-run-primary';
                    primary.textContent = historyType() === 'reply' ? runVideoTitle(run) : `${runPublisher(run)}-${runVideoTitle(run)}`;
                    id.className = 'dialog-run-id';
                    id.textContent = run.name;
                    box.append(primary, id);
                }
                $('dialogError').textContent = failed.map(item => `${item.run.name}：${item.message}`).join('\n');
                $('dialogError').hidden = false;
                $('dialogTitle').textContent = `${failed.length} 个任务删除失败，可重试`;
            } else closeDeleteDialog();
            if (deleted.length) showNotice(`已删除 ${deleted.length} 个任务的数据目录（不可恢复）`);
            await loadRuns()
        } catch (error) {
            showNotice(error.message, true)
        } finally {
            historyBusy = false;
            syncHistorySelection();
            $('confirmDelete').disabled = false;
            $('cancelDelete').disabled = false;
        }
    }

    /** 下载接口返回的 Excel，读取服务端文件名；非成功响应必须显示错误。 */
    async function downloadExcel(url, options = {}) {
        const response = await fetch(url, options);
        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.error || '导出失败');
        }
        const disposition = response.headers.get('Content-Disposition') || '';
        const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
        const filename = encoded ? decodeURIComponent(encoded[1]) : 'comments.xlsx';
        const blob = await response.blob(), objectUrl = URL.createObjectURL(blob), link = document.createElement('a');
        link.href = objectUrl;
        link.download = filename;
        document.body.append(link);
        link.click();
        link.remove();
        // 浏览器开始下载后再释放链接，避免异步下载读取不到 Blob。
        setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
    }

    /** 按冻结目标合并导出或逐个下载；失败逐项提示，不使用 ZIP。 */
    async function exportSelectedRuns(mode) {
        if (historyBusy || !exportTargets.length) return;
        const targets = exportTargets.slice();
        historyBusy = true;
        syncHistorySelection();
        let succeeded = 0;
        const failures = [];
        try {
            if (mode === 'merged') {
                await downloadExcel(`${historyApiRoot()}/export-merged`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({names: targets})
                });
                succeeded = targets.length;
            } else {
                for (const name of targets) {
                    try {
                        await downloadExcel(`${historyApiRoot()}/${encodeURIComponent(name)}/excel`);
                        succeeded++;
                    } catch (error) {
                        failures.push(`${name}：${error.message}`);
                    }
                }
            }
            if (succeeded) showNotice(mode === 'merged' ? '合并导出已生成' : `已发起 ${succeeded} 个 Excel 下载${targets.length > 1 ? '，如被拦截请允许多个文件下载' : ''}`);
            if (failures.length) showNotice(failures.join('\n'), true);
        } catch (error) {
            showNotice(error.message, true);
        } finally {
            historyBusy = false;
            exportTargets = [];
            syncHistorySelection();
        }
    }

    $('selectAllRuns').onchange = () => {
        checkedRuns.clear();
        if ($('selectAllRuns').checked) historyRuns.filter(run => !run.is_active).forEach(run => checkedRuns.add(run.name));
        syncHistorySelection();
    };
    $('deleteRuns').onclick = openDeleteDialog;

    /** 标题行导出图标直接采用指定方式，冻结当前勾选，不再询问导出方式。 */
    function startSelectedExport(mode) {
        if (historyBusy) return;
        exportTargets = historyRuns.filter(run => checkedRuns.has(run.name) && !run.is_active).map(run => run.name);
        return exportSelectedRuns(mode);
    }

    $('exportMerged').onclick = () => startSelectedExport('merged');
    $('exportSeparate').onclick = () => startSelectedExport('separate');

    $('confirmDelete').onclick = deleteRun;
    $('cancelDelete').onclick = closeDeleteDialog;
    $('deleteDialog').onclick = event => {
        if (event.target === $('deleteDialog')) closeDeleteDialog()
    };
    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        if ($('shareValidationDialog').classList.contains('show')) closeShareValidationDialog();
        else if ($('replyPresetNameDialog').classList.contains('show')) {
            if (!replyPresetBusy) closeReplyPresetNameDialog();
        } else if ($('replyPresetDeleteDialog').classList.contains('show')) {
            if (!replyPresetBusy) closeReplyPresetDeleteDialog();
        } else if ($('bulkChatDialog').classList.contains('show')) closeBulkChatDialog();
        else if ($('chatDialog').classList.contains('show')) closeChatDialog();
        else if ($('presetNameDialog').classList.contains('show')) {
            if (!presetBusy) closePresetNameDialog();
        } else if ($('presetDeleteDialog').classList.contains('show')) {
            if (!presetBusy) closePresetDeleteDialog();
        } else if ($('imagePreview').classList.contains('show')) closeImagePreview(); else if ($('deleteDialog').classList.contains('show')) closeDeleteDialog()
    });

    function openImagePreview(source, alt) {
        $('previewImage').src = source;
        $('previewImage').alt = alt || '评论图片预览';
        $('imagePreview').classList.add('show');
        $('imagePreview').setAttribute('aria-hidden', 'false');
        $('imagePreviewClose').focus()
    }

    function closeImagePreview() {
        $('imagePreview').classList.remove('show');
        $('imagePreview').setAttribute('aria-hidden', 'true');
        $('previewImage').removeAttribute('src')
    }

    $('imagePreviewClose').onclick = closeImagePreview;
    $('imagePreview').onclick = event => {
        if (event.target === $('imagePreview')) closeImagePreview()
    };

    /** 从当前结果类型读取预览，切换类型后的迟到响应一律丢弃。 */
    async function selectRun(run) {
        const expectedType = run.type === 'reply' ? 'reply' : 'capture';
        if (expectedType !== historyType()) return;
        if (selectedRun !== run.name) selectedChatUsers.clear();
        selectedRun = run.name;
        $("runTitle").textContent = run.name;
        $("runMeta").textContent = run.type === 'reply' ? `${run.comment_count} 条命中评论 · ${run.status || '状态未知'}` : `${run.comment_count} 条评论 · ${run.image_count} 张图片`;
        try {
            const suffix = run.type === 'reply' ? 'replies' : 'comments';
            const data = await api(`${historyApiRoot()}/${encodeURIComponent(run.name)}/${suffix}`);
            if (expectedType !== historyType() || selectedRun !== run.name) return;
            allComments = run.type === 'reply' ? (Array.isArray(data.matches) ? data.matches.map(item => ({
                ...item.comment,
                _reply: item
            })) : []) : (Array.isArray(data.comments) ? data.comments : []);
            renderComments();
            loadRuns()
        } catch (error) {
            showNotice(error.message, true)
        }
    }

    const loadSelectedRun = selectRun;
    selectRun = async run => {
        selectedRunInfo = run;
        await loadSelectedRun(run);
        if ((run.type === 'reply' ? 'reply' : 'capture') !== historyType() || selectedRun !== run.name) return;
        $("runTitle").textContent = run.type === 'reply' ? runVideoTitle(run) : `${runPublisher(run)} ${runVideoTitle(run)}`;
        $("runMeta").textContent = run.name
    };
    /** 切换结果类型时清除旧类型的预览、勾选和私信状态，避免跨类型误操作。 */
    $('historyTaskType').onchange = () => {
        if ($('deleteDialog').classList.contains('show')) closeDeleteDialog();
        selectedRun = '';
        selectedRunInfo = null;
        allComments = [];
        checkedRuns.clear();
        selectedChatUsers.clear();
        $('runTitle').textContent = '选择一个结果';
        $('runMeta').textContent = historyType() === 'reply' ? '查看匹配评论与回复结果' : '查看评论并下载 Excel';
        $('comments').innerHTML = '<div class="empty">从左侧选择一个结果</div>';
        $('historyChatActions').hidden = historyType() === 'reply';
        loadRuns();
    };

    function commentTimestamp(comment) {
        const seconds = Number(comment.create_time);
        return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : Date.parse(comment.create_time_iso || "")
    }

    function formatCommentTime(comment) {
        const date = new Date(commentTimestamp(comment));
        if (!Number.isFinite(date.getTime())) return "时间未知";
        const pad = value => String(value).padStart(2, "0");
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
    }

    function filtered() {
        const query = $("search").value.trim().toLowerCase();
        const after = $("commentAfter").value ? new Date($("commentAfter").value).getTime() : NaN;
        let result = allComments.filter(comment => {
            const author = comment.author?.nickname || "";
            const region = comment.ip_label || "";
            const match = !query || `${comment.text || ""} ${author} ${region}`.toLowerCase().includes(query);
            const createdAt = commentTimestamp(comment);
            const timeMatch = !Number.isFinite(after) || (Number.isFinite(createdAt) && createdAt > after);
            return match && timeMatch
        });
        if ($("sort").value === "likes") result.sort((a, b) => (b.likes || 0) - (a.likes || 0));
        if ($("sort").value === "newest") result.sort((a, b) => (commentTimestamp(b) || 0) - (commentTimestamp(a) || 0));
        return result
    }

    const selectedChatUsers = new Set();
    let bulkChatState = {status: 'idle', items: []}, bulkChatTargets = [], bulkChatRun = '', bulkChatRequest = '',
        bulkChatSubmitting = false, bulkChatMode = 'draft';

    /** 仅判断批次是否仍在处理，不把完成状态当成正在运行。 */
    function bulkChatRunning() {
        return ['running', 'stopping'].includes(bulkChatState.status);
    }

    /** 在当前任务内按 sec_uid 取一条代表评论，重复评论不会增加收件人数。 */
    function selectedChatRecipients() {
        const users = new Map();
        for (const comment of allComments) {
            const uid = String(comment.author?.sec_uid || '');
            if (comment.id && selectedChatUsers.has(uid) && !users.has(uid)) users.set(uid, {
                comment_id: String(comment.id),
                sec_uid: uid,
                nickname: comment.author.nickname || '匿名用户',
                unique_id: String(comment.author.unique_id || '')
            });
        }
        return [...users.values()];
    }

    /** 当前显示范围的全选与去重人数同步，筛选隐藏的已选用户仍在预览中明确展示。 */
    function syncChatSelection() {
        if (historyType() === 'reply') return;
        const users = selectedChatRecipients(),
            visible = [...new Set(filtered().filter(c => c.id && c.author?.sec_uid).map(c => String(c.author.sec_uid)))];
        $('chatSelectionCount').textContent = `已选 ${users.length} 位用户`;
        $('openBulkChat').disabled = !users.length && !bulkChatState.id;
        $('selectVisibleChatUsers').checked = visible.length > 0 && visible.every(uid => selectedChatUsers.has(uid));
        $('selectVisibleChatUsers').indeterminate = visible.some(uid => selectedChatUsers.has(uid)) && !$('selectVisibleChatUsers').checked;
        $('selectVisibleChatUsers').disabled = bulkChatRunning();
        document.querySelectorAll('[data-chat-user]').forEach(input => {
            input.disabled = bulkChatRunning() || !input.dataset.chatUser || !input.dataset.chatComment;
            input.checked = selectedChatUsers.has(input.dataset.chatUser);
        });
    }

    /** 按执行状态分类，不把需人工核对的提交当成成功送达。 */
    function bulkRecipientKind(item) {
        if (item.status === 'submitted' && item.page_closed === false) return 'review';
        return item.status || 'pending';
    }

    /** 预览保持名单顺序，执行结果按类型归组；轮询保留折叠状态且安全显示用户文本。 */
    function renderBulkRecipients(items) {
        const box = $('bulkChatRecipients'),
            expanded = new Map([...box.querySelectorAll('.bulk-chat-group')].map(group => [group.dataset.kind, group.open]));
        box.replaceChildren();
        const names = {
            running: '处理中',
            pending: '待发送',
            submitted: '已提交',
            review: '需人工核对',
            skipped: '无法私信（已跳过）',
            failed: '发送异常',
            cancelled: '未发送'
        };
        const groups = new Map();
        for (const item of items) {
            const kind = bulkRecipientKind(item);
            if (!groups.has(kind)) groups.set(kind, []);
            groups.get(kind).push(item);
        }
        // 发送前仅预览收件人，不为尚未执行的名单制造结果分类。
        const preview = items.every(item => !item.status);
        for (const kind of Object.keys(names)) {
            const entries = groups.get(kind);
            if (!entries?.length) continue;
            let container = box;
            if (!preview) {
                const group = document.createElement('details'), summary = document.createElement('summary'),
                    count = document.createElement('span');
                group.className = 'bulk-chat-group';
                group.dataset.kind = kind;
                // 新分类默认折叠，轮询时保留用户手动展开或收起的状态。
                group.open = expanded.has(kind) ? expanded.get(kind) : false;
                summary.textContent = names[kind];
                count.textContent = `${entries.length} 人`;
                summary.append(count);
                group.append(summary);
                box.append(group);
                container = group;
            }
            for (const item of entries) {
                const row = document.createElement('div'), title = document.createElement('div'),
                    detail = document.createElement('small');
                row.className = 'bulk-chat-recipient';
                title.textContent = `${item.nickname} · 抖音号：${item.unique_id || '未获取'}`;
                detail.textContent = item.detail || '';
                row.append(title, detail);
                container.append(row);
            }
        }
    }

    /** 有收件人和内容即可点击批量发送；打开弹窗及填写内容本身不会发送。 */
    function syncBulkChatControls() {
        const draft = bulkChatMode === 'draft';
        $('bulkChatCompose').hidden = !draft;
        $('startBulkChat').hidden = !draft;
        $('startBulkChat').disabled = bulkChatSubmitting || bulkChatRunning() || !bulkChatTargets.length || !$('bulkChatDraft').value.trim();
        $('bulkChatDraft').disabled = bulkChatSubmitting;
        $('stopBulkChat').hidden = !bulkChatRunning();
        $('stopBulkChat').disabled = bulkChatState.status === 'stopping';
        $('closeBulkChat').disabled = bulkChatSubmitting;
        // 后台逐个处理时保持提示可见，但不遮挡“停止后续发送”。
        $('bulkChatLoading').hidden = !(bulkChatSubmitting || bulkChatRunning());
        $('bulkChatLoadingTitle').textContent = bulkChatSubmitting ? '正在启动批量私信' : '正在处理批量私信';
    }

    /** 打开预览不发送；已有运行任务时展示该任务，不能用新的选择替换当前发送对象。 */
    function openBulkChatDialog() {
        if (chatBusy || bulkChatSubmitting) return;
        const targets = selectedChatRecipients();
        if (bulkChatRunning() || (!targets.length && bulkChatState.id)) {
            bulkChatMode = 'result';
            renderBulkRecipients(bulkChatState.items);
            $('bulkChatStatus').textContent = bulkChatState.message || '';
        } else {
            if (!targets.length) {
                showNotice('请先勾选评论用户', true);
                return;
            }
            bulkChatTargets = targets;
            bulkChatRun = selectedRun;
            bulkChatMode = 'draft';
            bulkChatRequest = crypto.randomUUID();
            renderBulkRecipients(targets);
            $('bulkChatStatus').textContent = '填写统一内容后，点击“批量发送”发送给以上用户。';
        }
        $('bulkChatStatus').className = 'chat-status';
        syncBulkChatControls();
        $('bulkChatDialog').classList.add('show');
        $('bulkChatDialog').setAttribute('aria-hidden', 'false');
    }

    /** 关闭时清空统一消息，但不停止已提交的批次；HTTP 提交期间禁止关闭。 */
    function closeBulkChatDialog() {
        if (bulkChatSubmitting) return;
        // 后台已接收的消息不受影响，仅清理弹窗草稿，避免下次误用旧内容。
        $('bulkChatDraft').value = '';
        $('bulkChatDialog').classList.remove('show');
        $('bulkChatDialog').setAttribute('aria-hidden', 'true');
        syncBulkChatControls();
    }

    /** 用固定请求标识确认启动一次，响应丢失时同一标识重试也不会再建批次。 */
    async function startBulkChat() {
        if ($('startBulkChat').disabled) return;
        bulkChatSubmitting = true;
        syncBulkChatControls();
        $('bulkChatStatus').textContent = '正在提交批量任务…';
        try {
            bulkChatState = await api('/api/private-chat/batch/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    run_name: bulkChatRun,
                    comment_ids: bulkChatTargets.map(u => u.comment_id),
                    message: $('bulkChatDraft').value,
                    request_id: bulkChatRequest
                })
            });
            bulkChatMode = 'result';
            renderBulkRecipients(bulkChatState.items);
            $('bulkChatStatus').textContent = bulkChatState.message;
        } catch (error) {
            $('bulkChatStatus').className = 'chat-status error';
            $('bulkChatStatus').textContent = `${error.message} 请先查看进度，勿重新建批次重复发送。`;
        } finally {
            bulkChatSubmitting = false;
            syncBulkChatControls();
            syncChatSelection();
            syncChatControls();
        }
    }

    /** 请求停止后续用户，当前已开始的提交不自动撤回或重发。 */
    async function stopBulkChat() {
        if (!bulkChatRunning()) return;
        $('stopBulkChat').disabled = true;
        try {
            bulkChatState = await api('/api/private-chat/batch/stop', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({job_id: bulkChatState.id})
            });
            $('bulkChatStatus').textContent = bulkChatState.message;
        } catch (error) {
            showNotice(error.message, true);
        } finally {
            syncBulkChatControls();
        }
    }

    /** 只读取任务状态，刷新不重新发送；不重建未提交的收件人预览。 */
    async function pollBulkChat() {
        try {
            bulkChatState = await api('/api/private-chat/batch');
            if (bulkChatState.id && bulkChatState.request_id === bulkChatRequest) bulkChatMode = 'result';
            const items = bulkChatState.items || [], done = items.filter(i => i.status === 'submitted').length;
            // 仅清空当前批次完全提交后的统一内容，异常或停止时保留用于人工核对。
            if (bulkChatMode === 'result' && bulkChatState.request_id === bulkChatRequest && bulkChatState.status === 'completed' && items.length && items.every(i => i.status === 'submitted')) $('bulkChatDraft').value = '';
            $('bulkChatSummary').textContent = bulkChatState.id ? `${bulkChatState.message} · 已提交 ${done}/${items.length}` : '';
            if (bulkChatMode === 'result') {
                renderBulkRecipients(items);
                $('bulkChatStatus').textContent = bulkChatState.message || '';
            }
            // 请求响应丢失但后台实际已接受时，以固定 request_id 对应的任务结果为准。
            syncChatSelection();
            syncBulkChatControls();
            syncChatControls();
        } catch {
        } finally {
            setTimeout(pollBulkChat, 1500);
        }
    }

    $('selectVisibleChatUsers').onchange = () => {
        for (const c of filtered()) {
            if (c.id && c.author?.sec_uid) {
                const uid = String(c.author.sec_uid);
                if ($('selectVisibleChatUsers').checked) selectedChatUsers.add(uid); else selectedChatUsers.delete(uid);
            }
        }
        syncChatSelection();
    };
    $('openBulkChat').onclick = openBulkChatDialog;
    $('closeBulkChat').onclick = closeBulkChatDialog;
    $('startBulkChat').onclick = startBulkChat;
    $('stopBulkChat').onclick = stopBulkChat;
    $('bulkChatDraft').oninput = syncBulkChatControls;
    $('bulkChatDialog').onclick = event => {
        if (event.target === $('bulkChatDialog')) closeBulkChatDialog();
    };

    let chatTarget = null, chatBusy = false, chatToken = null, chatSendAttempted = false;
    const chatDrafts = new Map();

    /** 操作期间展示独立的加载框，提示用户查看抖音窗口；完成或失败后立即收起。 */
    function setChatLoading(phase) {
        $('chatLoading').hidden = !phase;
        if (!phase) return;
        $('chatLoadingTitle').textContent = phase === 'send' ? '正在发送私信' : '正在打开抖音私信';
        $('chatLoadingHint').textContent = phase === 'send'
            ? '请查看另一个浏览器窗口，核对聊天窗口和发送结果。'
            : '请查看另一个浏览器窗口，按需完成登录或验证。';
    }

    /** 有有效目标和文本即可直接发送；实际发送前自动打开并核验会话。 */
    function syncChatControls() {
        $('openChatPage').disabled = chatBusy || bulkChatRunning() || !chatTarget?.sec_uid;
        $('sendChatMessage').disabled = chatBusy || bulkChatRunning() || chatSendAttempted || !chatTarget?.sec_uid || !$('chatDraft').value.trim();
        $('closeChat').disabled = chatBusy;
        $('chatDraft').disabled = chatBusy;
    }

    /** 显示固定评论的私聊目标，草稿按用户暂存内存，不影响采集和结果选择。 */
    function openChatDialog(comment) {
        if (chatBusy) return;
        chatToken = null;
        chatSendAttempted = false;
        setChatLoading(null);
        const author = comment.author || {};
        chatTarget = {
            run_name: selectedRun,
            comment_id: String(comment.id || ''),
            sec_uid: String(author.sec_uid || ''),
            nickname: author.nickname || '匿名用户'
        };
        $('chatTitle').textContent = `私聊：${chatTarget.nickname}`;
        $('chatIdentity').textContent = `抖音号：${String(author.unique_id || '').trim() || '未获取'}\n目标标识：${chatTarget.sec_uid || '缺少 sec_uid'}`;
        $('chatDraft').value = chatDrafts.get(chatTarget.sec_uid) || '';
        $('chatStatus').textContent = chatTarget.sec_uid ? '' : '缺少 sec_uid，无法确认目标用户。';
        $('chatStatus').className = 'chat-status';
        syncChatControls();
        $('chatDialog').classList.add('show');
        $('chatDialog').setAttribute('aria-hidden', 'false');
        $('chatDraft').focus();
    }

    /** 关闭私聊弹窗时清空输入与暂存草稿；操作过程中禁止关闭以防目标错配。 */
    function closeChatDialog() {
        if (chatBusy) return;
        // 同时删除暂存内容，避免再次打开同一用户时回显旧消息。
        if (chatTarget?.sec_uid) chatDrafts.delete(chatTarget.sec_uid);
        $('chatDraft').value = '';
        setChatLoading(null);
        $('chatDialog').classList.remove('show');
        $('chatDialog').setAttribute('aria-hidden', 'true');
        chatTarget = null;
        chatToken = null;
    }

    /** 打开入口仅提交任务和评论 ID，不上传消息，不触发发送。 */
    async function openChatPage() {
        if (chatBusy || !chatTarget?.sec_uid) return;
        const target = {...chatTarget};
        chatBusy = true;
        chatToken = null;
        syncChatControls();
        setChatLoading('open');
        $('chatStatus').className = 'chat-status';
        $('chatStatus').textContent = '正在独立标签页核对用户并打开私信入口…';
        try {
            const result = await api('/api/private-chat/open', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({run_name: target.run_name, comment_id: target.comment_id})
            });
            chatToken = result.send_ready ? result.chat_token || null : null;
            chatSendAttempted = false;
            $('chatStatus').textContent = result.message;
        } catch (error) {
            $('chatStatus').textContent = error.message;
            $('chatStatus').className = 'chat-status error';
        } finally {
            chatBusy = false;
            setChatLoading(null);
            syncChatControls();
        }
    }

    /** 用户点击才发送一条文本；凭据立即消费，网络异常不重发，失败仍保留草稿。 */
    async function sendChatMessage() {
        if (chatBusy || chatSendAttempted || !chatTarget?.sec_uid || !$('chatDraft').value.trim()) return;
        const target = {...chatTarget}, existingToken = chatToken, message = $('chatDraft').value;
        chatToken = null;
        chatBusy = true;
        syncChatControls();
        setChatLoading(existingToken ? 'send' : 'open');
        $('sendChatMessage').textContent = '发送中…';
        $('chatStatus').className = 'chat-status';
        $('chatStatus').textContent = '正在核对聊天对象并提交私信，请勿重复发送…';
        try {
            let token = existingToken;
            if (!token) {
                $('chatStatus').textContent = '正在自动打开抖音私信并核对目标…';
                const opened = await api('/api/private-chat/open', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({run_name: target.run_name, comment_id: target.comment_id})
                });
                if (!opened.send_ready || !opened.chat_token) throw new Error(opened.message || '无法核对聊天对象，未发送');
                token = opened.chat_token;
            }
            // 一旦开始提交，即使网络断开也不能安全补发；需先人工核对并重新打开。
            chatSendAttempted = true;
            setChatLoading('send');
            $('chatStatus').textContent = '正在提交私信，请勿重复发送…';
            const result = await api('/api/private-chat/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({chat_token: token, message})
            });
            $('chatStatus').textContent = result.message;
            // 输入框清空且标签页关闭才视为已确认提交；不确定结果保留草稿供核对。
            if (result.status === 'submitted' && result.page_closed) {
                $('chatDraft').value = '';
                chatDrafts.delete(target.sec_uid);
            }
            showNotice('已提交发送操作，请到抖音核对结果');
        } catch (error) {
            $('chatStatus').textContent = `${error.message} 不会自动重发；请先核对抖音实际结果。`;
            $('chatStatus').className = 'chat-status error';
        } finally {
            chatBusy = false;
            setChatLoading(null);
            $('sendChatMessage').textContent = '发送私信';
            syncChatControls();
        }
    }

    $('chatDraft').addEventListener('input', () => {
        if (chatTarget?.sec_uid) chatDrafts.set(chatTarget.sec_uid, $('chatDraft').value);
        syncChatControls();
    });
    $('closeChat').onclick = closeChatDialog;
    $('openChatPage').onclick = openChatPage;
    $('sendChatMessage').onclick = sendChatMessage;
    $('chatDialog').onclick = event => {
        if (event.target === $('chatDialog')) closeChatDialog();
    };

    /** 一次渲染筛选后的全部评论，图片仍懒加载，私聊图标跟随对应昵称。 */
    function renderComments() {
        if (historyType() !== 'reply') syncChatSelection();
        $("historyAdvancedLabel").textContent = $("commentAfter").value ? "高级筛选（时间筛选已启用）" : "高级筛选";
        const result = filtered(), box = $("comments");
        box.replaceChildren();
        if (!result.length) {
            box.innerHTML = '<div class="empty">没有符合条件的评论</div>';
            return
        }
        result.forEach(comment => {
            const card = document.createElement("article");
            card.className = "comment";
            if ((comment.level || 1) > 1) card.style.marginLeft = `${Math.min(36, ((comment.level || 2) - 1) * 18)}px`;
            const head = document.createElement("div");
            head.className = "comment-head";
            const author = document.createElement("strong");
            author.textContent = `${(comment.level || 1) > 1 ? `↳ ${comment.level}级回复 · ` : ""}${comment.author?.nickname || "匿名用户"}`;
            const place = document.createElement("span");
            place.textContent = [formatCommentTime(comment), comment.ip_label].filter(Boolean).join(" · ");
            const actions = document.createElement('div'), chat = document.createElement('button');
            actions.className = 'comment-head-actions';
            chat.type = 'button';
            chat.className = 'icon-button comment-chat';
            chat.title = comment.author?.sec_uid ? '私聊此用户（不自动发送）' : '缺少 sec_uid，无法定位用户';
            chat.setAttribute('aria-label', `私聊 ${comment.author?.nickname || '匿名用户'}`);
            chat.disabled = !comment.author?.sec_uid;
            chat.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-3 2v-10a8.5 8.5 0 0 1 8.5-8.5h3a8.5 8.5 0 0 1 8.5 8.5Z"/><path d="M7 11h8M7 15h5"/></svg>';
            const authorBox = document.createElement('div'), selectUser = document.createElement('input');
            authorBox.className = 'comment-author-selection';
            selectUser.type = 'checkbox';
            selectUser.className = 'run-check';
            selectUser.dataset.chatUser = String(comment.author?.sec_uid || '');
            selectUser.checked = selectedChatUsers.has(selectUser.dataset.chatUser);
            selectUser.dataset.chatComment = String(comment.id || '');
            selectUser.disabled = bulkChatRunning() || !comment.id || !selectUser.dataset.chatUser;
            selectUser.setAttribute('aria-label', `选择私信用户 ${comment.author?.nickname || '匿名用户'}`);
            selectUser.onchange = () => {
                if (selectUser.checked) selectedChatUsers.add(selectUser.dataset.chatUser); else selectedChatUsers.delete(selectUser.dataset.chatUser);
                syncChatSelection();
            };
            if (historyType() === 'reply') authorBox.append(author);
            else authorBox.append(selectUser, author, chat);
            chat.onclick = () => openChatDialog(comment);
            actions.append(place);
            head.append(authorBox, actions);
            const account = document.createElement("div");
            account.className = "comment-account";
            account.textContent = `抖音号：${String(comment.author?.unique_id || "").trim() || "未获取"}`;
            const text = document.createElement("div");
            text.className = "comment-text";
            text.textContent = comment.text || "（无文字）";
            const metrics = document.createElement("div");
            metrics.className = "metrics";
            metrics.textContent = `♥ ${comment.likes || 0}　回复 ${comment.reply_count || 0}${comment.parent_comment_id ? `　父评论 ${comment.parent_comment_id}` : ""}`;
            card.append(head, account, text, metrics);
            if (comment._reply) {
                const outcome = document.createElement('div');
                outcome.className = 'reply-outcome';
                const labels = {
                    matched: '已匹配',
                    waiting_for_reply: '等待人工回复',
                    sending: '发送中',
                    sent: '已提交',
                    failed: '回复失败',
                    uncertain: '结果待核对',
                    not_found_on_page: '页面未找到评论',
                    stopped: '已停止'
                };
                outcome.textContent = `回复状态：${labels[comment._reply.status] || comment._reply.status || '未知'}${comment._reply.reply_text ? ` · 回复内容：${comment._reply.reply_text}` : ''}${comment._reply.error ? ` · ${comment._reply.error}` : ''}`;
                card.append(outcome);
            }
            const files = historyType() === 'capture' ? (comment.images || []).filter(image => image.file) : [];
            if (files.length) {
                const images = document.createElement("div");
                images.className = "images";
                files.forEach(image => {
                    const img = document.createElement("img");
                    img.loading = "lazy";
                    img.alt = "评论图片";
                    img.src = `/api/runs/${encodeURIComponent(selectedRun)}/files/${encodeURIComponent(image.file)}`;
                    img.onclick = () => openImagePreview(img.src, img.alt);
                    images.append(img)
                });
                card.append(images)
            }
            box.append(card)
        });
    }

    ["search", "sort", "commentAfter"].forEach(id => $(id).addEventListener(["search", "commentAfter"].includes(id) ? "input" : "change", () => {
        renderComments()
    }));
    $("clear").onclick = () => {
        $("search").value = "";
        $("sort").value = "source";
        $("commentAfter").value = "";
        renderComments()
    };

    /** 手动刷新成功时滑出提示，等待期间禁用按钮，自动刷新不打扰用户。 */
    async function refreshHistoryRuns() {
        if ($("refresh").disabled) return;
        $("refresh").disabled = true;
        try {
            if (await loadRuns()) showNotice("刷新成功");
        } finally {
            $("refresh").disabled = false;
        }
    }

    $("refresh").onclick = refreshHistoryRuns;

