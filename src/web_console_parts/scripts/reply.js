    let replyBatchId = '', replyPollBusy = false, replyWaitingId = '', replyWaitingJob = '', replySubmittedId = '',
        replyRunning = false, replyStartPending = false;
    let replyKeywords = [];
    let replyPresetData = {selected: '', presets: []}, replyPresetBusy = false, replyViewedPreset = '',
        replyEditing = false, replyCreating = false;
    let activeReplySettings = {}, replyPresetDeleteTarget = null;
    const replyLogCards = new Map();
    let replyInitialBatchChecked = false, replyHiddenBatchId = '';

    /** 刷新只恢复仍在运行的批次；记住旧批次，防止后续轮询重新填回完成记录。 */
    function visibleReplyTasks(state) {
        const active = ['starting', 'running'].includes(state.status);
        if (!replyInitialBatchChecked) {
            replyInitialBatchChecked = true;
            replyHiddenBatchId = active ? '' : state.batch_id;
        }
        return !active && state.batch_id === replyHiddenBatchId ? [] : state.tasks;
    }

    /** 创建与采集页相同的分享文本输入行；回复方式和文案由整批公共设置决定。 */
    function addReplyVideo(data = {}) {
        if ($('replyVideos').children.length >= 100) {
            showNotice('最多添加 100 个视频链接', true);
            return null;
        }
        const root = document.createElement('div');
        // 与采集页共用列表和输入行样式，不额外包裹视频标题卡片。
        root.className = 'video-link-row';
        const linkRow = root;
        const remove = document.createElement('button'), add = document.createElement('button');
        remove.type = add.type = 'button';
        remove.className = 'video-link-add video-link-remove';
        add.className = 'video-link-add';
        remove.textContent = '−';
        add.textContent = '+';
        remove.title = '删除此视频链接';
        add.title = '新增一行视频链接';
        remove.setAttribute('aria-label', '删除此视频链接');
        add.setAttribute('aria-label', '新增视频链接');
        remove.onclick = () => {
            const next = root.previousElementSibling || root.nextElementSibling;
            root.remove();
            syncReplyVideoRows();
            next?.replyFields.share.focus();
        };
        add.onclick = () => {
            const next = addReplyVideo();
            next?.replyFields.share.focus();
        };
        const share = document.createElement('input');
        share.type = 'text';
        share.maxLength = 16384;
        share.autocomplete = 'off';
        share.placeholder = '请粘贴抖音完整分享文本';
        share.value = data.share_text || '';
        share.setAttribute('aria-label', '抖音视频链接');
        share.oninput = () => {
            share.removeAttribute('aria-invalid');
            checkDuplicateReplyVideos(share);
        };
        linkRow.append(share, remove, add);
        $('replyVideos').append(root);
        root.replyFields = {linkRow, remove, add, share};
        syncReplyVideoRows();
        return root;
    }

    /** 与采集页相同：单行只显示加号，非末行只显示减号，末行两个都显示。 */
    function syncReplyVideoRows() {
        const rows = [...$('replyVideos').children];
        rows.forEach((row, index) => {
            const fields = row.replyFields;
            fields.remove.hidden = rows.length === 1;
            fields.add.hidden = index !== rows.length - 1;
            fields.linkRow.classList.toggle('removable', rows.length > 1 && index === rows.length - 1);
            fields.share.disabled = replyRunning;
            fields.remove.disabled = replyRunning;
            fields.add.disabled = replyRunning;
            if (index === 0) fields.share.id = 'replyShareText'; else fields.share.removeAttribute('id');
        });
    }

    /** 按分享链接或视频 ID 对比输入，重复时清空后输入的那一行。 */
    function checkDuplicateReplyVideos(changed = null) {
        const inputs = [...$('replyVideos').querySelectorAll('.video-link-row input')], seen = new Map(),
            duplicates = [];
        for (const [index, input] of inputs.entries()) {
            if (changed && input !== changed) continue;
            const key = shareTextKey(input.value);
            if (!key) continue;
            const previous = changed ? inputs.findIndex(other => other !== input && shareTextKey(other.value) === key) : seen.get(key);
            if (previous !== undefined && previous !== -1) {
                duplicates.push(`第 ${index + 1} 条与第 ${previous + 1} 条重复`);
                input.value = '';
                input.removeAttribute('aria-invalid');
            } else seen.set(key, index);
        }
        if (duplicates.length) showNotice(`${duplicates.join('；')}，已清空重复输入框`, true);
        return duplicates.length > 0;
    }

    /** 只读取视频卡里的分享文本；回复方式和文案来自当前应用方案，预览草稿不能生效。 */
    function replyVideoPayload() {
        const mode = activeReplySettings.mode === 'automatic' ? 'automatic' : 'manual';
        const replyText = typeof activeReplySettings.reply_text === 'string' ? activeReplySettings.reply_text.trim() : '';
        return [...$('replyVideos').children].map(row => ({
            share_text: row.replyFields.share.value,
            mode,
            reply_text: replyText
        }));
    }

    /** 关键词以去重列表展示，输入框内容不作为尚未确认的筛选条件。 */
    function renderReplyKeywords() {
        const box = $('replyKeywords');
        box.replaceChildren();
        for (const term of replyKeywords) {
            const chip = document.createElement('span'), label = document.createElement('span'),
                remove = document.createElement('button');
            chip.className = 'reply-keyword-chip';
            label.textContent = term;
            remove.type = 'button';
            remove.textContent = '×';
            remove.setAttribute('aria-label', `删除关键词 ${term}`);
            remove.disabled = !replyEditing || replyPresetBusy || replyRunning;
            remove.onclick = () => {
                replyKeywords = replyKeywords.filter(value => value !== term);
                renderReplyKeywords();
            };
            chip.append(label, remove);
            box.append(chip);
        }
    }

    /** 回车或添加键确认一条关键词，最多 50 条。 */
    function addReplyKeyword() {
        const term = $('replyKeyword').value.trim();
        if (!term) return;
        if (replyKeywords.length >= 50) {
            showNotice('最多添加 50 个关键词', true);
            return;
        }
        if (!replyKeywords.some(value => value.toLocaleLowerCase() === term.toLocaleLowerCase())) replyKeywords.push(term);
        $('replyKeyword').value = '';
        renderReplyKeywords();
    }

    $('addReplyKeyword').onclick = addReplyKeyword;

    let replyRegions = [], replyRegionOpen = false, replyRegionActive = 0;

    /** 与后端同一规则：去掉行政区后缀，输入“广东省”也能匹配候选“广东”。 */
    function regionKey(value) {
        const label = String(value || '').replace(/\s+/g, '');
        for (const suffix of ['特别行政区', '维吾尔自治区', '壮族自治区', '回族自治区', '自治区', '省', '市']) {
            if (label.endsWith(suffix)) return label.slice(0, -suffix.length);
        }
        return label;
    }

    /**
     * 原生 datalist 弹层由浏览器绘制，无法用页面样式覆盖（深色系统下固定黑底），
     * 所以候选列表改为自绘：输入即过滤、↑↓ 移动、回车确认、Esc 或点外部关闭。
     */
    function closeReplyRegionList() {
        replyRegionOpen = false;
        $('replyRegionOptions').hidden = true;
        $('replyRegion').setAttribute('aria-expanded', 'false');
        $('replyRegion').removeAttribute('aria-activedescendant');
    }

    /** 未输入或输入已是完整地区时列出全部候选（便于改选），输入不完整时按前缀过滤。 */
    function renderReplyRegionOptions() {
        const box = $('replyRegionOptions'), value = regionKey($('replyRegion').value);
        const term = replyRegions.includes(value) ? '' : value;
        const options = (term ? [] : [{value: '', label: '不限地区'}])
            .concat(replyRegions.filter(region => region.startsWith(term))
                .map(region => ({value: region, label: region})));
        box.replaceChildren();
        for (const option of options) {
            const item = document.createElement('li'), current = option.value && option.value === value;
            item.id = `replyRegionOption-${box.children.length}`;
            item.dataset.value = option.value;
            item.setAttribute('role', 'option');
            item.setAttribute('aria-selected', String(Boolean(current)));
            item.textContent = option.label;
            if (current) {
                const check = document.createElement('span');
                check.className = 'combo-check';
                check.textContent = '✓';
                item.classList.add('is-current');
                item.append(check);
            }
            item.onclick = () => pickReplyRegion(option.value);
            box.append(item);
        }
        return box.children.length;
    }

    /** 高亮项同时写入 aria-activedescendant，键盘与读屏使用同一状态。 */
    function setReplyRegionActive(index) {
        const items = [...$('replyRegionOptions').children];
        if (!items.length) return;
        replyRegionActive = (index + items.length) % items.length;
        items.forEach((item, position) => item.classList.toggle('is-active', position === replyRegionActive));
        $('replyRegion').setAttribute('aria-activedescendant', items[replyRegionActive].id);
        items[replyRegionActive].scrollIntoView({block: 'nearest'});
    }

    function openReplyRegionList() {
        const input = $('replyRegion');
        if (input.disabled) return;
        if (!renderReplyRegionOptions()) return closeReplyRegionList();
        replyRegionOpen = true;
        $('replyRegionOptions').hidden = false;
        input.setAttribute('aria-expanded', 'true');
        const selected = [...$('replyRegionOptions').children].findIndex(item => item.classList.contains('is-current'));
        setReplyRegionActive(selected === -1 ? 0 : selected);
    }

    /** 选中只写回输入框内容，地区合法性仍由后端校验。 */
    function pickReplyRegion(value) {
        $('replyRegion').value = value;
        closeReplyRegionList();
        $('replyRegion').focus();
    }

    $('replyRegion').oninput = openReplyRegionList;
    $('replyRegion').onclick = event => {
        const input = event.currentTarget, box = input.getBoundingClientRect();
        // 右侧箭头是开合开关（热区与样式表 padding-right: 40px 一致）；其余区域用于继续输入。
        if (replyRegionOpen && event.clientX - box.left >= box.width - 40) return closeReplyRegionList();
        openReplyRegionList();
    };
    $('replyRegion').onkeydown = event => {
        if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
            event.preventDefault();
            if (!replyRegionOpen) return openReplyRegionList();
            setReplyRegionActive(replyRegionActive + (event.key === 'ArrowDown' ? 1 : -1));
        } else if (event.key === 'Enter' && replyRegionOpen) {
            event.preventDefault();
            const item = $('replyRegionOptions').children[replyRegionActive];
            if (item) pickReplyRegion(item.dataset.value);
        } else if (event.key === 'Escape' && replyRegionOpen) {
            closeReplyRegionList();
        } else if (event.key === 'Tab') {
            closeReplyRegionList();
        }
    };
    document.addEventListener('click', event => {
        if (!$('replyRegionCombo').contains(event.target)) closeReplyRegionList();
    });
    $('replyKeyword').onkeydown = event => {
        if (event.key === 'Enter') {
            event.preventDefault();
            addReplyKeyword();
        }
    };

    /** 读取当前可编辑的公共回复条件；视频行不属于命名方案。 */
    function currentReplySettings() {
        return {
            region: $('replyRegion').value.trim(),
            keywords: [...replyKeywords],
            source_mode: $('replySourceMode').value,
            mode: $('replyMode').value,
            reply_text: $('replyPresetText').value.trim(),
            concurrent: $('replyConcurrent').checked,
            concurrency_limit: Number($('replyConcurrencyLimit').value),
            retry_limit: readRetryLimit('replyRetryLimit'),
            max_minutes: Number($('replyMaxMinutes').value)
        };
    }

    /** 回显某个方案，始终不改变各视频的分享文本。 */
    function applyReplySettings(settings) {
        closeReplyRegionList();
        $('replyRegion').value = settings.region || '';
        replyKeywords = [...(settings.keywords || [])];
        renderReplyKeywords();
        $('replyKeyword').value = '';
        $('replySourceMode').value = settings.source_mode || 'page_first';
        $('replyMode').value = settings.mode === 'automatic' ? 'automatic' : 'manual';
        $('replyPresetText').value = settings.reply_text || '';
        syncReplyDraftField();
        $('replyMaxMinutes').value = settings.max_minutes || 30;
        $('replyConcurrent').checked = settings.concurrent === true;
        $('replyConcurrencyLimit').value = settings.concurrency_limit ?? 2;
        $('replyRetryLimit').value = settings.retry_limit ?? 3;
        syncReplyConcurrencyOptions();
        centerSelectArrows();
    }

    /** 并发关闭时隐藏数量，保留草稿中的数量供再次开启时使用。 */
    function syncReplyConcurrencyOptions() {
        $('replyConcurrencyOptions').hidden = !$('replyConcurrent').checked;
    }

    $('replyConcurrent').onchange = syncReplyConcurrencyOptions;

    /** 只有“预设文字自动回复”需要文案，与服务端校验保持一致。 */
    function syncReplyDraftField() {
        $('replyDraftField').hidden = $('replyMode').value !== 'automatic';
    }

    $('replyMode').onchange = syncReplyDraftField;

    /** 查看只读、编辑解锁；回复任务运行时所有方案修改保持禁用。 */
    function syncReplyPresetControls() {
        const disabled = replyPresetBusy || replyRunning;
        $('startReply').disabled = disabled || replyStartPending;
        $('replyPresetManager').querySelectorAll('button').forEach(button => {
            button.disabled = replyPresetBusy || (replyRunning && button.dataset.replyPresetView !== 'true') || button.dataset.permanentlyDisabled === 'true';
        });
        for (const id of ['replyRegion', 'replyKeyword', 'addReplyKeyword', 'replyMaxMinutes', 'replySourceMode', 'replyMode', 'replyPresetText', 'replyConcurrent', 'replyConcurrencyLimit', 'replyRetryLimit']) $(id).disabled = disabled || !replyEditing;
        // 查看态或运行中禁用地区输入框时，已展开的候选列表不能留在页面上。
        if ($('replyRegion').disabled) closeReplyRegionList();
        $('replyKeywords').querySelectorAll('button').forEach(button => button.disabled = disabled || !replyEditing);
        $('saveReplyPreset').hidden = !replyCreating;
        $('updateReplyPreset').hidden = !replyEditing || replyCreating;
        $('cancelReplyPresetEdit').hidden = !replyEditing;
        $('cancelReplyPresetEdit').textContent = replyCreating ? '取消' : '取消编辑';
        for (const id of ['saveReplyPreset', 'updateReplyPreset', 'cancelReplyPresetEdit', 'confirmReplyPresetName', 'confirmReplyPresetDelete']) $(id).disabled = disabled;
        $('cancelReplyPresetName').disabled = replyPresetBusy;
        $('cancelReplyPresetDelete').disabled = replyPresetBusy;
        $('replyPresetName').disabled = disabled;
    }

    /** 默认配置与命名方案沿用采集页的查看、编辑、应用和删除语义。 */
    function canDeleteReplyPreset(name) {
        const hasDefault = replyPresetData.default_enabled !== false;
        const exists = name ? replyPresetData.presets.some(item => item.name === name) : hasDefault;
        return exists && name !== replyPresetData.selected && replyPresetData.presets.length + Number(hasDefault) > 1;
    }

    /** 方案表格最后一行只提供新增入口，当前使用项不可删除或再次应用。 */
    function renderReplyPresetRows() {
        const body = $('replyPresetRows');
        body.replaceChildren();
        for (const preset of [...(replyPresetData.default_enabled === false ? [] : [{name: ''}]), ...replyPresetData.presets]) {
            const row = document.createElement('tr'), name = document.createElement('td'),
                state = document.createElement('td'), operations = document.createElement('td');
            row.className = [preset.name === replyPresetData.selected ? 'preset-current' : '', preset.name === replyViewedPreset ? 'preset-viewed' : ''].join(' ');
            name.textContent = preset.name || '默认配置';
            const current = preset.name === replyPresetData.selected, badge = document.createElement('span');
            badge.className = 'preset-badge' + (current ? '' : ' preset-badge-inactive');
            badge.textContent = current ? '当前使用' : '未使用';
            state.append(badge);
            const actions = document.createElement('div');
            actions.className = 'preset-row-actions';
            for (const [label, action] of [['查看', () => viewReplyPreset(preset.name)], ['编辑', () => viewReplyPreset(preset.name, true)], ['应用', () => applyReplyPreset(preset.name)], ['删除', () => openReplyPresetDeleteDialog(preset.name)]]) {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'btn' + (label === '删除' ? ' danger' : '');
                button.textContent = label;
                if (label === '查看') button.dataset.replyPresetView = 'true';
                if ((label === '删除' && !canDeleteReplyPreset(preset.name)) || (label === '应用' && current)) button.dataset.permanentlyDisabled = 'true';
                button.title = label === '删除' && !canDeleteReplyPreset(preset.name) ? (current ? '当前使用的配置不能删除' : '至少保留一个配置方案') : '';
                button.onclick = action;
                actions.append(button);
            }
            operations.append(actions);
            row.append(name, state, operations);
            body.append(row);
        }
        const row = document.createElement('tr'), cell = document.createElement('td'),
            add = document.createElement('button');
        cell.colSpan = 3;
        cell.className = 'preset-add-cell';
        add.type = 'button';
        add.className = 'preset-add';
        add.textContent = '+';
        add.setAttribute('aria-label', '新增回复配置方案');
        add.onclick = beginCreateReplyPreset;
        cell.append(add);
        row.append(cell);
        body.append(row);
        syncReplyPresetControls();
    }

    /** 方案预览不应用；已选中的方案显示真正生效的设置。 */
    function viewReplyPreset(name, edit = false) {
        if (replyPresetBusy || (edit && replyRunning)) return;
        const preset = replyPresetData.presets.find(item => item.name === name);
        if (name ? !preset : replyPresetData.default_enabled === false) return;
        replyViewedPreset = name;
        replyCreating = false;
        replyEditing = edit;
        applyReplySettings(name === replyPresetData.selected ? activeReplySettings : name ? preset.settings : replyPresetData.default_settings);
        $('replyPresetDetailTitle').textContent = `${edit ? '编辑' : '查看'}：${name || '默认配置'}`;
        $('replyPresetHint').textContent = `当前使用：${replyPresetData.selected || '默认配置'}。查看其他方案不会切换回复配置。`;
        renderReplyPresetRows();
    }

    /** 新增草稿从内置默认值开始，不复制当前条件，也不清空视频行。 */
    function beginCreateReplyPreset() {
        if (replyPresetBusy || replyRunning) return;
        replyCreating = true;
        replyEditing = true;
        replyViewedPreset = '';
        applyReplySettings(replyPresetData.defaults);
        $('replyPresetDetailTitle').textContent = '新增方案';
        renderReplyPresetRows();
    }

    /** 接收服务端原子保存结果并默认预览当前应用配置。 */
    function renderReplyPresets(data, view = null) {
        replyPresetData = data;
        activeReplySettings = {...data.settings, keywords: [...data.settings.keywords]};
        replyPresetBusy = false;
        viewReplyPreset(view === null ? data.selected : view);
    }

    /** 读取已保存回复方案；刷新页面不改变任何视频的发布状态。 */
    async function loadReplyPresets() {
        try {
            renderReplyPresets(await api('/api/comment-replies/presets'));
        } catch (error) {
            showNotice(`读取回复配置方案失败：${error.message}`, true);
        }
    }

    /** 与采集页一致：明确点击“应用”后才改变下一次回复的公共条件。 */
    function applyReplyPreset(name) {
        return manageReplyPreset(name ? 'select' : 'custom', name);
    }

    /** 只提交一套方案的公共字段；失败时保留当前预览供用户修正。 */
    async function manageReplyPreset(action, name, saveDefault = false) {
        if (replyPresetBusy || replyRunning) return false;
        if (['create', 'update'].includes(action) || action === 'custom' && saveDefault) {
            if ($('replyKeyword').value.trim()) addReplyKeyword();
        }
        replyPresetBusy = true;
        syncReplyPresetControls();
        try {
            const settings = ['create', 'update'].includes(action) || saveDefault ? currentReplySettings() : null;
            const data = await api('/api/comment-replies/presets', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action, name, settings})
            });
            const view = ['create', 'update'].includes(action) ? name : data.selected;
            renderReplyPresets(data, view);
            showNotice({
                create: '回复方案已保存',
                update: '回复方案已更新',
                select: '回复方案已应用',
                delete: '回复方案已删除',
                custom: '默认配置已应用'
            }[action]);
            return true;
        } catch (error) {
            showNotice(error.message, true);
            return false;
        } finally {
            replyPresetBusy = false;
            syncReplyPresetControls();
        }
    }

    /** 创建方案时弹窗输入名称，确认后再保存。 */
    function openReplyPresetNameDialog() {
        if (!replyCreating || replyPresetBusy || replyRunning) return;
        $('replyPresetName').value = '';
        $('replyPresetNameError').hidden = true;
        $('replyPresetNameDialog').classList.add('show');
        $('replyPresetNameDialog').setAttribute('aria-hidden', 'false');
        $('replyPresetName').focus();
    }

    /** 关闭名称弹窗但保留尚未保存的方案草稿。 */
    function closeReplyPresetNameDialog() {
        $('replyPresetNameDialog').classList.remove('show');
        $('replyPresetNameDialog').setAttribute('aria-hidden', 'true');
    }

    /** 名称由服务端再次校验，前端先给出直接可读的错误。 */
    async function confirmReplyPresetName() {
        const name = $('replyPresetName').value.trim();
        if (!name || name.length > 50 || replyPresetData.presets.some(item => item.name === name)) {
            $('replyPresetNameError').textContent = !name ? '请输入配置方案名称' : name.length > 50 ? '名称不能超过 50 个字符' : '同名方案已存在';
            $('replyPresetNameError').hidden = false;
            return;
        }
        if (await manageReplyPreset('create', name)) closeReplyPresetNameDialog();
    }

    /** 删除弹窗固定原方案名，切换查看状态不会误删其他方案。 */
    function openReplyPresetDeleteDialog(name) {
        if (replyPresetBusy || replyRunning || !canDeleteReplyPreset(name)) return;
        replyPresetDeleteTarget = name;
        $('replyPresetDeleteName').textContent = name || '默认配置';
        $('replyPresetDeleteDialog').classList.add('show');
        $('replyPresetDeleteDialog').setAttribute('aria-hidden', 'false');
        $('cancelReplyPresetDelete').focus();
    }

    /** 关闭删除确认，不修改持久化方案。 */
    function closeReplyPresetDeleteDialog() {
        $('replyPresetDeleteDialog').classList.remove('show');
        $('replyPresetDeleteDialog').setAttribute('aria-hidden', 'true');
        replyPresetDeleteTarget = null;
    }

    $('replySettingsToggle').onclick = () => {
        const panel = $('replySettingsPanel'), open = panel.hidden;
        panel.hidden = !open;
        $('replySettingsToggle').setAttribute('aria-expanded', String(open));
        $('replySettingsToggleLabel').textContent = open ? '收起　⌃' : '展开　⌄';
    };
    $('saveReplyPreset').onclick = openReplyPresetNameDialog;
    $('updateReplyPreset').onclick = () => manageReplyPreset(replyViewedPreset ? 'update' : 'custom', replyViewedPreset, !replyViewedPreset);
    $('cancelReplyPresetEdit').onclick = () => viewReplyPreset(replyCreating ? replyPresetData.selected : replyViewedPreset);
    $('confirmReplyPresetName').onclick = confirmReplyPresetName;
    $('cancelReplyPresetName').onclick = () => {
        if (!replyPresetBusy) closeReplyPresetNameDialog();
    };
    $('replyPresetNameDialog').onclick = event => {
        if (event.target === $('replyPresetNameDialog') && !replyPresetBusy) closeReplyPresetNameDialog();
    };
    $('replyPresetName').onkeydown = event => {
        if (event.key === 'Enter') {
            event.preventDefault();
            confirmReplyPresetName();
        }
    };
    $('confirmReplyPresetDelete').onclick = async () => {
        if (replyPresetDeleteTarget !== null && await manageReplyPreset('delete', replyPresetDeleteTarget)) closeReplyPresetDeleteDialog();
    };
    $('cancelReplyPresetDelete').onclick = closeReplyPresetDeleteDialog;
    $('replyPresetDeleteDialog').onclick = event => {
        if (event.target === $('replyPresetDeleteDialog') && !replyPresetBusy) closeReplyPresetDeleteDialog();
    };

    /** 载入运行中的多视频配置与独立 XPath；刷新不重新启动或发送。 */
    async function loadReplySettings() {
        const state = await api('/api/comment-replies'), xpaths = await api('/api/comment-replies/xpaths');
        replyRegions = [...state.regions];
        replyRunning = ['starting', 'running'].includes(state.status);
        const restoredTasks = visibleReplyTasks(state);
        $('replyVideos').replaceChildren();
        if (replyRunning && restoredTasks.length) restoredTasks.forEach(task => addReplyVideo(task)); else addReplyVideo();
        renderReplyLogs(restoredTasks);
        renderReplyTasks(restoredTasks, replyRunning);
        renderReplyStatus(state, restoredTasks);
        for (const [key, id] of Object.entries({
            comment_card_xpath: 'replyCardXpath',
            reply_button_xpath: 'replyButtonXpath',
            reply_editor_xpath: 'replyEditorXpath',
            reply_send_xpath: 'replySendXpath'
        })) $(id).value = xpaths[key] || '';
        await loadReplyPresets();
    }

    /** 多个视频各有自己的日志框；用户翻看旧日志时不强制滚到底部。 */
    function renderReplyLogs(tasks) {
        const box = $('replyLogs');
        if (!tasks.length) {
            box.replaceChildren();
            replyLogCards.clear();
            const empty = document.createElement('div');
            empty.className = 'log-empty';
            empty.textContent = '暂无回复任务，启动后按视频分别显示日志。';
            box.append(empty);
            return;
        }
        box.querySelector('.log-empty')?.remove();
        const labels = {pending: '等待回复', running: '回复中', waiting_for_reply: '等待确认',
            stopping: '正在停止', complete: '已完成', failed: '失败', stopped: '已停止', interrupted: '已中断'};
        for (const [index, task] of tasks.entries()) {
            let card = replyLogCards.get(task.job_id);
            if (!card) {
                const root = document.createElement('section'), head = document.createElement('div'),
                    name = document.createElement('strong'), status = document.createElement('span'),
                    url = document.createElement('div'), body = document.createElement('div');
                root.className = 'task-log-card';
                head.className = 'task-log-header';
                status.className = 'task-log-state';
                url.className = 'task-log-url';
                body.className = 'task-log-body';
                head.append(name, status);
                root.append(head, url, body);
                box.append(root);
                card = {root, name, status, url, body};
                replyLogCards.set(task.job_id, card);
            }
            card.name.textContent = `视频 ${index + 1}`;
            card.status.textContent = `${labels[task.status] || task.status} · 命中 ${task.total ?? 0} 条`;
            card.url.textContent = task.video_url || task.run_name || task.job_id;
            const logs = task.logs || [];
            card.body.dataset.empty = String(!logs.length);
            // 复用采集页的底部跟随逻辑，翻看旧日志时保持各自滚动位置。
            updateLogBox(card.body, logs, task.message || '暂无日志');
        }
        for (const [id, card] of replyLogCards) if (!tasks.some(task => task.job_id === id)) {
            card.root.remove();
            replyLogCards.delete(id);
        }
    }

    /** 将回复批次状态映射到采集页共用的圆点样式；隐藏的旧批次恢复为等待开始。 */
    function renderReplyStatus(state, tasks) {
        const active = ['starting', 'running'].includes(state.status);
        let status = '', message = '等待开始';
        if (active) {
            status = 'running';
            message = state.status === 'starting' ? '正在准备回复任务' : '正在筛选评论并回复';
            if (tasks.some(task => task.status === 'waiting_for_reply')) message = '等待逐条确认回复';
            else if (tasks.some(task => task.status === 'stopping')) message = '正在停止回复任务';
        } else if (tasks.length) {
            if (tasks.some(task => task.status === 'failed')) {
                status = 'failed';
                message = '评论回复任务已结束，存在失败任务';
            } else if (tasks.some(task => ['stopped', 'cancelled', 'interrupted'].includes(task.status))) {
                message = '评论回复任务已结束，部分或全部任务已停止';
            } else {
                status = 'succeeded';
                message = '评论回复任务已完成';
            }
        }
        $('replyStatusDot').className = `dot${status ? ' ' + status : ''}`;
        // 相同轮询结果不重复改写，避免读屏持续播报相同状态。
        if ($('replyStatusText').textContent !== message) $('replyStatusText').textContent = message;
    }

    /** 共用采集队列外观，每个停止按钮只提交对应视频的任务 ID。 */
    function renderReplyTasks(tasks, active) {
        const pending = new Set(['pending', 'starting', 'running', 'waiting_for_reply', 'stopping']);
        const labels = {pending: '等待', starting: '正在启动', running: '回复中',
            waiting_for_reply: '等待确认', stopping: '正在停止', complete: '完成',
            failed: '失败', stopped: '已停止', cancelled: '未启动', interrupted: '已中断'};
        $('replyBatchProgress').hidden = !tasks.length;
        const remaining = tasks.filter(task => pending.has(task.status)).length;
        $('replyBatchSummary').textContent = tasks.length
            ? `共 ${tasks.length} 个 · 已结束 ${tasks.length - remaining} 个 · 未完成 ${remaining} 个` : '';
        const box = $('replyBatchItems');
        box.replaceChildren();
        tasks.forEach((task, index) => {
            const row = document.createElement('div'), head = document.createElement('div'),
                title = document.createElement('strong'), actions = document.createElement('div'),
                status = document.createElement('span'), stop = document.createElement('button'),
                url = document.createElement('span');
            row.className = `batch-item ${task.status === 'complete' ? 'succeeded' : task.status}`;
            head.className = 'batch-item-head';
            actions.className = 'queue-actions';
            title.textContent = `视频 ${index + 1}`;
            status.textContent = labels[task.status] || task.status;
            stop.type = 'button';
            stop.className = 'icon-button queue-stop';
            stop.title = '停止此视频回复';
            stop.setAttribute('aria-label', `停止视频 ${index + 1} 回复`);
            stop.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>';
            stop.hidden = !pending.has(task.status);
            stop.disabled = !active || !pending.has(task.status) || task.status === 'stopping';
            // 不使用批次 ID，避免单条停止误伤整批或其他并发视频。
            stop.onclick = async () => {
                stop.disabled = true;
                try {
                    await api('/api/comment-replies/stop', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({job_id: task.job_id})
                    });
                    showNotice('正在停止此视频回复');
                    await pollReplyQueue();
                } catch (error) {
                    stop.disabled = false;
                    showNotice(error.message, true);
                }
            };
            url.className = 'batch-item-url';
            url.textContent = task.video_url || task.share_text || task.job_id;
            url.title = url.textContent;
            actions.append(status, stop);
            head.append(title, actions);
            row.append(head, url);
            if (task.error) {
                const error = document.createElement('div');
                error.className = 'batch-item-error';
                error.textContent = task.error;
                row.append(error);
            }
            box.append(row);
        });
    }

    /** 并发任务可同时等待人工输入；弹窗逐条展示，提交后显示下一条。 */
    function syncReplyDialog(state) {
        const waiting = state.waiting;
        if (!waiting) {
            $('replyDialog').classList.remove('show');
            $('replyDialog').setAttribute('aria-hidden', 'true');
            replyWaitingId = '';
            replyWaitingJob = '';
            replySubmittedId = '';
            return;
        }
        if (waiting.comment_id === replyWaitingId && waiting.job_id === replyWaitingJob) return;
        replyWaitingId = waiting.comment_id;
        replyWaitingJob = waiting.job_id;
        replySubmittedId = '';
        $('replyDialogAuthor').textContent = `${waiting.comment?.author?.nickname || '匿名用户'} · ${waiting.comment?.ip_label || '属地未知'}`;
        $('replyDialogComment').textContent = waiting.comment?.text || '（无文字）';
        $('replyDraft').value = '';
        $('replyDialogError').hidden = true;
        $('submitReply').disabled = false;
        $('skipReply').disabled = false;
        $('replyDialog').classList.add('show');
        $('replyDialog').setAttribute('aria-hidden', 'false');
        $('replyDraft').focus();
    }

    /** 轮询只回显批次状态和逐视频日志，不加载全部匹配评论。 */
    async function pollReplyQueue() {
        if (replyPollBusy) return;
        replyPollBusy = true;
        try {
            const state = await api('/api/comment-replies');
            if (state.batch_id !== replyBatchId) {
                replyBatchId = state.batch_id;
                replyLogCards.clear();
                $('replyLogs').replaceChildren();
            }
            const visibleTasks = visibleReplyTasks(state);
            renderReplyLogs(visibleTasks);
            const active = ['starting', 'running'].includes(state.status);
            renderReplyTasks(visibleTasks, active);
            renderReplyStatus(state, visibleTasks);
            replyRunning = active;
            $('stopReply').disabled = !active;
            $('replyVideos').querySelectorAll('textarea,input,button').forEach(input => input.disabled = active);
            syncReplyPresetControls();
            // 被刷新隐藏的旧批次也不能重新弹出上一轮的确认窗口。
            syncReplyDialog(visibleTasks.length ? state : {...state, waiting: null});
        } catch (error) {
            showNotice(error.message, true);
        } finally {
            replyPollBusy = false;
        }
    }

    /** 保存独立 XPath 设置，运行中的页面不允许切换定位规则。 */
    $('saveReplyXpaths').onclick = async () => {
        const payload = {
            comment_card_xpath: $('replyCardXpath').value,
            reply_button_xpath: $('replyButtonXpath').value,
            reply_editor_xpath: $('replyEditorXpath').value,
            reply_send_xpath: $('replySendXpath').value
        };
        try {
            await api('/api/comment-replies/xpaths', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            showNotice('回复 XPath 已保存');
        } catch (error) {
            showNotice(error.message, true);
        }
    };
    /** 全部视频与各自模式一次提交，服务端全量校验通过才打开页面。 */
    $('startReply').onclick = async () => {
        if (replyPresetBusy || replyStartPending || replyRunning) return;
        if (checkDuplicateReplyVideos()) return;
        replyStartPending = true;
        syncReplyPresetControls();
        // 启动只提交当前应用方案；查看中的其他方案和编辑草稿不能偷偷生效。
        const payload = {videos: replyVideoPayload(), ...activeReplySettings};
        try {
            await api('/api/comment-replies/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            replyRunning = true;
            showNotice('评论回复任务已启动');
            await pollReplyQueue();
        } catch (error) {
            showNotice(error.message, true);
        } finally {
            replyStartPending = false;
            syncReplyPresetControls();
        }
    };
    /** 请求停止整批回复；已点击发送但结果不明的记录不会重试。 */
    $('stopReply').onclick = async () => {
        $('stopReply').disabled = true;
        try {
            await api('/api/comment-replies/stop', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({job_id: replyBatchId})
            });
            showNotice('正在停止回复任务');
        } catch (error) {
            showNotice(error.message, true);
            $('stopReply').disabled = false;
        }
    };

    /** 手动确认只提交当前等待评论的一条回复。 */
    async function answerReply(skip) {
        if (!replyWaitingId || replySubmittedId === `${replyWaitingJob}:${replyWaitingId}`) return;
        const text = $('replyDraft').value.trim();
        if (!skip && !text) {
            $('replyDialogError').textContent = '请填写回复内容';
            $('replyDialogError').hidden = false;
            return;
        }
        $('submitReply').disabled = true;
        $('skipReply').disabled = true;
        try {
            await api(skip ? '/api/comment-replies/skip' : '/api/comment-replies/submit', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({job_id: replyWaitingJob, comment_id: replyWaitingId, text})
            });
            replySubmittedId = `${replyWaitingJob}:${replyWaitingId}`;
            $('replyDialog').classList.remove('show');
            $('replyDialog').setAttribute('aria-hidden', 'true');
        } catch (error) {
            $('replyDialogError').textContent = error.message;
            $('replyDialogError').hidden = false;
            $('submitReply').disabled = false;
            $('skipReply').disabled = false;
        }
    }

    $('submitReply').onclick = () => answerReply(false);
    $('skipReply').onclick = () => answerReply(true);

