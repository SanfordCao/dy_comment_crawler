    /** 为同一页面的独立控件生成纯文本引导；不读取或修改表单值。 */
    function tutorialInputSteps(view, rows, options = {}) {
        const dialogFallbacks = {
            presetNameDialog: '#savePreset:not([hidden]), #presetManager',
            replyPresetNameDialog: '#saveReplyPreset:not([hidden]), #replyPresetManager',
            presetDeleteDialog: '#presetManager', replyPresetDeleteDialog: '#replyPresetManager',
            shareValidationDialog: '#start', replyDialog: '#replyLogs',
            chatDialog: '#comments', bulkChatDialog: '#openBulkChat', deleteDialog: '#deleteRuns'
        };
        return rows.map(([id, title, body, target, extra]) => {
            const step = {
                id, title, body, target: target || `#${id}`, fallback: `#${view}`,
                view, ...options, ...extra
            };
            if (dialogFallbacks[step.dialog] && !extra?.fallback) step.fallback = dialogFallbacks[step.dialog];
            return step;
        });
    }

    /** 复用采集和回复方案流程；动态按钮顺序与方案渲染器保持一致。 */
    function tutorialSchemeSteps(reply, phase) {
        const view = reply ? 'reply' : 'crawl', rows = reply ? 'replyPresetRows' : 'presetRows';
        const expand = reply ? 'replySettingsToggle' : 'advancedToggle';
        const nameDialog = reply ? 'replyPresetNameDialog' : 'presetNameDialog';
        const deleteDialog = reply ? 'replyPresetDeleteDialog' : 'presetDeleteDialog';
        const dynamic = {missing: '方案列表加载后显示；引导不会自动新增、编辑、应用或删除方案。'};
        const entries = phase === 'before' ? [
            ['schemeCurrent', '当前使用方案', '“当前使用”标记表示真正生效的方案；查看其他方案不会切换它。', `#${rows} .preset-current`, dynamic],
            ['schemeView', '查看方案', '只预览该方案，不改变当前使用配置。', `#${rows} .preset-row-actions button:nth-of-type(1)`, dynamic],
            ['schemeEdit', '编辑方案', '手动点击编辑后才能修改方案参数；运行任务时部分操作不可用。', `#${rows} .preset-row-actions button:nth-of-type(2)`, dynamic],
            ['schemeNew', '新增方案', '加号进入新增配置状态，不会直接创建或应用方案。', `#${rows} .preset-add`, dynamic]
        ] : [
            ['schemeUpdate', '保存修改', '编辑已有方案后保存修改；请核对参数后自行操作。', reply ? '#updateReplyPreset' : '#updatePreset', {missing: '手动编辑已有方案后显示。'}],
            ['schemeSave', '保存新方案', '新增方案参数填写完成后，点击这里打开命名窗口。', reply ? '#saveReplyPreset' : '#savePreset', {missing: '手动新增方案后显示。'}],
            ['schemeName', '方案名称', '输入便于识别的名称，最多 50 字。保存方案不会自动应用。', reply ? '#replyPresetName' : '#presetName', {dialog: nameDialog, missing: '手动打开保存方案窗口后显示。'}],
            ['schemeNameSave', '确认保存名称', '确认名称和参数后保存到本机；引导不会代为保存。', reply ? '#confirmReplyPresetName' : '#confirmPresetName', {dialog: nameDialog}],
            ['schemeNameCancel', '取消命名', '关闭命名窗口，不保存这次新方案。', reply ? '#cancelReplyPresetName' : '#cancelPresetName', {dialog: nameDialog}],
            ['schemeCancel', '取消编辑', '放弃当前编辑并退出编辑状态；请留意尚未保存的参数。', reply ? '#cancelReplyPresetEdit' : '#cancelPresetEdit', {missing: '进入编辑或新增状态后显示。'}],
            ['schemeApply', '应用方案', '切换真正生效的配置；当前使用方案无需重复应用。请自行确认后操作。', `#${rows} .preset-row-actions button:nth-of-type(3)`, dynamic],
            ['schemeDelete', '删除方案入口', '仅删除保存的方案，不删除任务结果；当前使用方案和唯一剩余方案不能删除。', `#${rows} .preset-row-actions button:nth-of-type(4)`, dynamic],
            ['schemeDeleteCancel', '取消删除方案', '保留方案并关闭确认窗口。', reply ? '#cancelReplyPresetDelete' : '#cancelDeletePreset', {dialog: deleteDialog}],
            ['schemeDeleteConfirm', '确认删除方案', '确认名称后才可删除；这是实际删除操作，引导不会点击。', reply ? '#confirmReplyPresetDelete' : '#confirmDeletePreset', {dialog: deleteDialog}]
        ];
        return tutorialInputSteps(view, entries, {expand});
    }

    const TUTORIAL_MODULES = [
        {
            id: 'capture', title: '评论采集', description: '从完整分享文本到方案、队列和人工验证，逐项认识采集功能。',
            steps: [
                ...tutorialInputSteps('crawl', [
                    ['links', '填写分享文本', '粘贴抖音完整分享文本，不接受单独网址；空行会忽略，全部有效后才启动。', '#videoLinks input'],
                    ['linkAdd', '新增视频输入行', '加号添加另一条分享文本，可批量配置多个视频。', '#videoLinks .video-link-row:last-child .video-link-add:not(.video-link-remove)'],
                    ['linkRemove', '移除视频输入行', '减号仅移除这一输入行，不删除历史结果。', '#videoLinks .video-link-remove', {missing: '有多行分享文本时显示减号；无需为演示增加输入行。'}],
                    ['settings', '展开或收起设置', '设置折叠后仍可启动或停止任务；折叠不会清空配置。', '#advancedToggle']
                ]),
                ...tutorialSchemeSteps(false, 'before'),
                ...tutorialInputSteps('crawl', [
                    ['commentScope', '评论层级', '选择仅一级评论或所有层级；所有层级会运行至完整加载。'],
                    ['fetchMode', '一级评论加载方式', '接口优先失败时自动回到页面滚动，也可只使用页面滚动。'],
                    ['maxComments', '最多评论数', '0 表示不限数量；仅一级评论模式使用此上限，所有层级模式忽略此上限并运行至完整加载。'],
                    ['fetchDouyinId', '获取抖音号', '开启后通过主页接口补充缺失抖音号，同一用户只查一次；会增加访问和耗时。'],
                    ['concurrent', '并发采集开关', '默认顺序采集；并发会增加资源占用及验证概率。遇到验证会暂停启动新任务。'],
                    ['concurrencyLimit', '同时采集的视频数', '范围 1–100，默认 2；实际不超过待采集视频数。', null, {fallback: 'label[for="concurrent"]', missing: '手动开启并发采集后显示；引导不会切换开关。'}],
                    ['maxMinutes', '最长运行时间', '单位为分钟；仅一级评论模式使用此时长上限，所有层级模式忽略此上限并运行至完整加载。'],
                    ['retryLimit', '失败重试次数', '默认 3，范围 0–10；首次尝试不计入次数。达到上限跳过失败项，列表无法推进时停止当前视频并保留已有数据。'],
                    ['direction', '加载方向', '选择页面滚动加载方向。'],
                    ['minDelay', '最短滚动等待', '单位毫秒，设置滚动间隔下限；不要通过极短等待追求速度。'],
                    ['maxDelay', '最长滚动等待', '单位毫秒，应不小于最短滚动等待。'],
                    ['waitMinutes', '登录等待时间', '等待人工登录或验证的时间上限，单位分钟。']
                ], {expand: 'advancedToggle'}),
                ...tutorialSchemeSteps(false, 'after'),
                ...tutorialInputSteps('crawl', [
                    ['start', '启动采集任务', '点击会校验全部分享文本并创建真实任务；引导不会启动任务。'],
                    ['validation', '校验问题列表', '任一分享文本无效时不会启动任何采集；按问题修改再启动。', '#shareValidationIssues', {dialog: 'shareValidationDialog', missing: '仅校验失败时显示，不会为演示触发校验。'}],
                    ['validationClose', '返回修改', '关闭校验提示后修改对应分享文本。', '#closeShareValidationDialog', {dialog: 'shareValidationDialog'}],
                    ['queue', '采集队列', '逐个查看任务状态、视频和失败原因。', '#batchProgress', {missing: '已有本轮采集任务时显示。'}],
                    ['stopOne', '停止单个视频', '队列中的停止按钮只停止对应视频；请谨慎手动操作。', '#batchItems .queue-stop', {missing: '队列生成后显示；可停止与否取决于任务状态。'}],
                    ['stop', '停止全部采集', '停止本轮采集，等待当前操作结束并保存已采集数据。'],
                    ['continue', '登录或验证完成后继续', '在程序打开的 Chrome 完成人工登录或验证后再点击继续。', null, {missing: '任务需要人工操作时显示。'}],
                    ['logs', '采集运行日志', '运行后每个视频分别显示日志，排查进度、等待和错误；不会因查看而启动任务。', '#taskLogs', {fallback: '#logEmpty', missing: '暂无任务时显示空日志提示。'}]
                ])
            ]
        },
        {
            id: 'reply', title: '评论回复', description: '了解筛选、回复方案和逐条确认，所有发布都由你决定。',
            steps: [
                ...tutorialInputSteps('reply', [
                    ['links', '回复视频分享文本', '填写完整分享文本；全部通过校验后才开始处理，空行忽略。', '#replyShareText'],
                    ['linkAdd', '新增回复视频', '末行加号添加新的视频输入行，最多 100 条。', '#replyVideos .video-link-row:last-child .video-link-add:not(.video-link-remove)'],
                    ['linkRemove', '移除回复视频', '仅移除输入行，不删除既有回复结果。', '#replyVideos .video-link-remove', {missing: '有多行分享文本时显示。'}],
                    ['settings', '回复设置折叠', '折叠不清空设置，任务启动和停止按钮仍然可见。', '#replySettingsToggle']
                ]),
                ...tutorialSchemeSteps(true, 'before'),
                ...tutorialInputSteps('reply', [
                    ['replyRegion', 'IP 属地筛选', '输入省级地区并从列表选择；依据接口 IP 属地匹配，不推断城市或实际位置。'],
                    ['regionOptions', '选择候选地区', '在候选列表选择有效地区，而不是仅输入任意文字。', '#replyRegionOptions', {fallback: '#replyRegion', missing: '手动展开地区输入框的候选列表后显示。'}],
                    ['replyKeyword', '评论关键词', '填写需要匹配的关键词，再点击添加。'],
                    ['addReplyKeyword', '添加关键词', '多个关键词满足任意一个即可；同时设置地区时，还必须满足地区。'],
                    ['removeKeyword', '移除关键词', '已添加关键词旁的按钮用于移除该条件。', '#replyKeywords button', {fallback: '#replyKeyword', missing: '添加至少一个关键词后显示。'}],
                    ['replySourceMode', '评论加载方式', '当前只开放页面元素优先滚动加载；接口优先暂未开放。'],
                    ['replyMode', '回复方式', '整批统一选择实时逐条确认，或预设文字自动回复；自动模式会真实发布。'],
                    ['replyPresetText', '预设回复文字', '自动回复时必填，最多 500 字；整批命中评论使用同一文字，不确定结果不会自动重试。', null, {fallback: '#replyMode', missing: '手动选择预设文字自动回复后显示。'}],
                    ['replyConcurrent', '并发回复', '默认按顺序处理视频；开启后每个视频独立保存结果和日志。'],
                    ['replyConcurrencyLimit', '回复并发视频数', '范围 1–100，默认 2，实际不超过待处理视频数。', null, {fallback: 'label[for="replyConcurrent"]', missing: '手动开启并发回复后显示。'}],
                    ['replyMaxMinutes', '回复最长运行时间', '单位分钟，范围 1–240；请按任务规模设置。'],
                    ['replyRetryLimit', '失败重试次数', '默认 3，范围 0–10；只重试加载与发布前的定位，不会自动重发评论或私信。']
                ], {expand: 'replySettingsToggle'}),
                ...tutorialSchemeSteps(true, 'after'),
                ...tutorialInputSteps('reply', [
                    ['startReply', '启动回复任务', '点击会校验分享文本并启动真实回复任务；自动模式会按预设内容发布，引导不会启动。'],
                    ['logs', '回复运行日志', '按视频查看运行状态、命中和发布结果。', '#replyLogs'],
                    ['manualComment', '核对待回复评论', '实时逐条确认模式命中后显示原评论和作者；先核对对象再写回复。', '#replyDialogComment', {dialog: 'replyDialog', missing: '实际任务等待人工确认时才显示。'}],
                    ['replyDraft', '填写本条回复', '输入不超过 500 字的内容；填写草稿本身不会发布。', null, {dialog: 'replyDialog'}],
                    ['submitReply', '回复并发布', '这会真实发布到目标评论，请核对内容后自行点击；引导不会代为发布。', null, {dialog: 'replyDialog'}],
                    ['skipReply', '跳过本条', '跳过当前命中评论，不发布本条回复。', null, {dialog: 'replyDialog'}],
                    ['stopReply', '停止回复任务', '停止当前批次后续处理；已经发布的回复不会撤回。']
                ])
            ]
        },
        {
            id: 'results', title: '结果与导出', description: '浏览、筛选和导出结果，认识删除及单条、批量私信的操作边界。',
            steps: tutorialInputSteps('history', [
                ['historyTaskType', '选择结果类型', '切换评论采集或评论回复历史；两类结果分别展示。'],
                ['refresh', '刷新历史任务', '重新读取已有任务列表，不会启动新采集或回复。'],
                ['historyList', '打开历史任务', '点击任务行查看该次保存结果。', '#runs .run', {fallback: '#runs', missing: '有历史结果时显示可选任务行。'}],
                ['titleId', '作者与视频标题', '作者和视频标题帮助识别内容；相同视频可能有多次任务，请再核对任务 ID。', '#runTitle'],
                ['runMeta', '任务 ID', '这里显示当前任务 ID，用于区分同一视频的多次任务；它不是视频 ID。'],
                ['search', '搜索评论', '按评论、作者或地区缩小当前结果范围。'],
                ['sort', '结果排序', '可按原始顺序、点赞最多或最新发布查看。'],
                ['historyAdvancedToggle', '高级筛选开关', '展开时间筛选；收起不会自动清除筛选条件。'],
                ['commentAfter', '评论时间下限', '使用本机时区，只显示此时间之后的评论；留空不过滤时间。', null, {expand: 'historyAdvancedToggle'}],
                ['clear', '清空筛选', '清除当前搜索和时间筛选，重新查看结果。', null, {expand: 'historyAdvancedToggle'}],
                ['selectRun', '勾选任务', '左侧复选框用于导出或删除选择，和点击任务行查看是两种操作。', '#runs input[data-run-name]', {fallback: '#runs', missing: '有历史任务时显示；活动任务不可选择删除。'}],
                ['selectAllRuns', '全选任务', '选择当前列表可选任务，操作前核对已选数量。'],
                ['exportMerged', '合并导出', '将所选任务合并成一个文件下载；先勾选任务。'],
                ['exportSeparate', '分别下载', '将所选任务分别下载为多个文件。'],
                ['deleteRuns', '删除所选任务', '打开删除确认窗口；将永久删除所选任务目录的数据、图片和导出文件。'],
                ['deleteNames', '核对删除目标', '逐项核对确认窗口中的作者、视频标题和任务 ID，避免删除同一视频的其他任务。', '#dialogRunName', {dialog: 'deleteDialog'}],
                ['cancelDelete', '取消删除结果', '关闭确认窗口并保留结果。', null, {dialog: 'deleteDialog'}],
                ['confirmDelete', '永久删除结果', '真实且不可恢复的删除操作；引导不会确认删除。', null, {dialog: 'deleteDialog'}],
                ['images', '评论图片', '有图片的评论会显示已保存图片；没有图片时不显示此区域。', '#comments .images', {fallback: '#comments', missing: '选择包含图片的结果后显示。'}],
                ['imagePreviewClose', '关闭图片预览', '查看评论图片后，点击关闭按钮返回结果列表；不会删除图片。', '#imagePreviewClose', {dialog: 'imagePreview', fallback: '#comments', missing: '手动打开评论图片预览后显示。'}],
                ['replyStatus', '回复结果状态', '切换评论回复结果后，可查看发布、跳过、失败或结果不确定等状态。', '#comments .reply-outcome', {fallback: '#comments', missing: '仅评论回复历史中的结果显示。'}],
                ['privateChat', '单条私信入口', '采集评论作者旁的图标打开私聊窗口；请先核对身份并遵守对方意愿。', '#comments .comment-chat', {fallback: '#comments', missing: '选择采集结果且存在评论时显示；回复结果不提供该入口。'}],
                ['recipient', '核对私信收件人', '确认收件人身份，勿仅凭相似昵称发送。', '#chatIdentity', {dialog: 'chatDialog'}],
                ['chatDraft', '私信草稿', '最多 1000 字；只填写草稿不会发送。', null, {dialog: 'chatDialog'}],
                ['openChatPage', '只打开抖音私信', '打开目标会话供人工查看，不自动发送草稿；需要正确配置私信 XPath。', null, {dialog: 'chatDialog'}],
                ['sendChatMessage', '发送单条私信', '自动打开并核对目标会话后提交一条消息，不自动重试。仅使用自己的账号，确认后自行发送。', null, {dialog: 'chatDialog'}],
                ['closeChat', '关闭私聊窗口', '关闭本地窗口不撤回已经发送的消息。', null, {dialog: 'chatDialog'}],
                ['selectUser', '选择私信用户', '评论作者旁复选框按用户选择；缺少身份信息时不可选。', '#comments input[data-chat-user]', {fallback: '#comments', missing: '仅采集结果中的评论显示用户选择框。'}],
                ['selectVisibleChatUsers', '选择当前显示用户', '选择当前筛选后显示的用户，同一用户只处理一次。'],
                ['openBulkChat', '打开批量私信', '选择用户后可打开批量确认窗口；此按钮本身不会发送。'],
                ['bulkRecipients', '核对批量收件人', '核对全部用户及分组状态；每位用户只发送一次，逐个处理。', '#bulkChatRecipients', {dialog: 'bulkChatDialog'}],
                ['bulkChatDraft', '统一私信内容', '向所选用户发送相同文本，最多 1000 字；请勿发送骚扰或垃圾信息。', null, {dialog: 'bulkChatDialog'}],
                ['startBulkChat', '批量发送', '会开始向全部所选用户真实发送，受平台验证和私信权限限制；引导不会点击。', null, {dialog: 'bulkChatDialog'}],
                ['stopBulkChat', '停止后续发送', '停止后续用户的发送，已发送消息不会撤回。', null, {dialog: 'bulkChatDialog', missing: '批量发送运行时显示。'}],
                ['bulkResults', '查看批量处理结果', '查看发送状态与收件人结果；不确定或失败时先人工核查，不要盲目重发。', '#bulkChatStatus', {dialog: 'bulkChatDialog'}],
                ['closeBulkChat', '关闭批量窗口', '任务运行时请先按界面提示停止或等待完成；关闭不会撤回消息。', null, {dialog: 'bulkChatDialog'}]
            ])
        },
        {
            id: 'advanced', title: '高级配置', description: '逐项了解页面 XPath 与接口路径；只在理解目标节点后修改。',
            steps: [
                ...tutorialInputSteps('xpathConfig', [
                    ['xpathCategory', '采集 XPath 分类', '配置暂停及单条、批量私信共用的页面节点。'],
                    ['pauseXpath', '暂停按钮 XPath', '打开视频、登录或验证完成后用于暂停；留空跳过，失败记录日志并继续。'],
                    ['privateChatXpath', '私信入口 XPath', '用于用户主页私信入口；必须填写并保存，不会自动匹配。先核对主页身份。'],
                    ['sendMessageXpath', '私信发送按钮 XPath', '用于聊天界面发送按钮；未配置时不会填写或发送消息。'],
                    ['closeChatPanelXpath', '消息面板关闭 XPath', '首次打开未进入会话时用于关闭再补点入口；留空停止恢复，不强制点击。'],
                    ['saveXpath', '保存采集 XPath', '保存全局配置；会同时保存采集接口字段，新任务及私信使用此配置。'],
                    ['clearXpath', '清空采集 XPath 表单', '只清空此分类表单中的四个 XPath；如需持久化还需自行保存。']
                ], {category: 'xpathCategory'}),
                ...tutorialInputSteps('xpathConfig', [
                    ['otherCategory', '采集接口分类', '监听响应并读取当前视频信息，只接受视频 ID 与任务一致的数据，不回退 HTML。'],
                    ['authorApiPath', '作者昵称接口路径', '只填写关键路径，不含域名或查询参数，例如 /aweme/v1/web/aweme/detail/。'],
                    ['authorJsonPath', '作者昵称 JSON 路径', '例如 aweme_detail.author.nickname；支持点分对象路径和数字数组索引。'],
                    ['titleApiPath', '视频标题接口路径', '填写返回视频标题的接口关键路径；接口或 JSON 路径留空会停用该字段。'],
                    ['titleJsonPath', '视频标题 JSON 路径', '例如 aweme_detail.item_title；应与实际接口数据结构一致。'],
                    ['saveApiSettings', '保存采集接口配置', '与采集 XPath 共用全局保存，请一并核对另一分类尚未保存的编辑。'],
                    ['clearApiSettings', '清空接口表单', '只清空此分类四个接口字段，不自动保存；确认停用后再自行保存。']
                ], {category: 'otherCategory'}),
                ...tutorialInputSteps('xpathConfig', [
                    ['replyXpathCategory', '回复 XPath 分类', '按评论 ID 定位容器，再在同一卡片内寻找回复、输入和发送节点。此分类没有清空按钮。'],
                    ['replyCardXpath', '评论容器 XPath', '必须包含 {comment_id}，以准确定位待回复评论。'],
                    ['replyButtonXpath', '回复按钮 XPath', '相对于评论容器，必须以 .// 开头。'],
                    ['replyEditorXpath', '回复输入框 XPath', '相对于评论容器，以 .// 开头；页面变化时需重新核对。'],
                    ['replySendXpath', '发布按钮 XPath', '相对于评论容器，以 .// 开头；错误定位可能影响真实发布。'],
                    ['saveReplyXpaths', '保存回复 XPath', '保存这四项回复定位配置，不会启动任务或发布评论。']
                ], {category: 'replyXpathCategory'})
            ]
        }
    ];
