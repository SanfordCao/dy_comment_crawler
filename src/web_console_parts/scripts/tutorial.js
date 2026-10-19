    /** 手动逐步教程：只导航和说明，不提交业务请求、不更改业务表单。 */
    (() => {
        const drawer = $('tutorialDrawer'), trigger = $('openTutorial');
        const coach = $('tutorialCoachmark'), modulesBox = $('tutorialModules');
        const storageKey = 'douyin-workbench.tutorial.v1';
        const allowedViews = new Set(['crawl', 'reply', 'history', 'xpathConfig']);
        const allowedCategories = new Set(['xpathCategory', 'otherCategory', 'replyXpathCategory']);
        const allowedExpanders = new Set(['advancedToggle', 'replySettingsToggle', 'historyAdvancedToggle']);
        let progress = readProgress(), activeModule = null, stepIndex = 0, complete = false;
        let previousFocus = null, highlighted = null, placementFrame = 0;
        let spacedDialog = null;

        /** 小屏为底部卡片预留空间，确保页面末尾及弹窗按钮仍能滚动到提示上方。 */
        function reserveCoachSpace(dialog = null) {
            const enabled = Boolean(activeModule && !coach.hidden && window.innerWidth <= 600);
            if (document.body.classList.contains('tutorial-bottom-space') !== enabled) {
                document.body.classList.toggle('tutorial-bottom-space', enabled);
            }
            const nextDialog = enabled ? dialog : null;
            if (spacedDialog !== nextDialog) {
                spacedDialog?.classList.remove('tutorial-dialog-space');
                spacedDialog = nextDialog;
                spacedDialog?.classList.add('tutorial-dialog-space');
            }
            if (enabled) document.body.style.setProperty('--tutorial-clearance', `${Math.ceil(coach.getBoundingClientRect().height) + 32}px`);
            else document.body.style.removeProperty('--tutorial-clearance');
        }

        /** 只恢复当前教程仍存在的稳定步骤 ID，损坏记录或受限存储退回内存。 */
        function readProgress() {
            try {
                const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
                if (saved?.version !== 1 || typeof saved.modules !== 'object' || !saved.modules) return {};
                const result = {};
                for (const module of TUTORIAL_MODULES) {
                    const entry = saved.modules[module.id];
                    if (entry && module.steps.some(step => step.id === entry.stepId)) {
                        result[module.id] = {stepId: entry.stepId, completed: entry.completed === true};
                    }
                }
                return result;
            } catch { return {}; }
        }

        /** 每次翻步即保存阅读位置，不保存任何分享文本、配置或消息内容。 */
        function rememberProgress() {
            if (!activeModule) return;
            progress[activeModule.id] = {stepId: activeModule.steps[stepIndex].id, completed: complete};
            try { localStorage.setItem(storageKey, JSON.stringify({version: 1, modules: progress})); }
            catch { /* 隐私模式禁止存储时，本次页面内仍可继续阅读。 */ }
        }

        /** 统一更新入口状态，包括暂时被业务弹窗盖住的教程。 */
        function syncExpanded() {
            trigger.setAttribute('aria-expanded', String(!drawer.hidden || Boolean(activeModule)));
        }

        /** 高亮只有一个目标；动态节点被替换后移除旧节点上的样式。 */
        function highlight(element) {
            if (highlighted === element) return;
            highlighted?.classList.remove('tutorial-highlight');
            highlighted = element;
            highlighted?.classList.add('tutorial-highlight');
        }

        /** 在保留进度的同时释放定位监听，退出不会回滚实际操作。 */
        function stopGuide() {
            rememberProgress();
            activeModule = null;
            observer.disconnect();
            sizeObserver?.disconnect();
            cancelAnimationFrame(placementFrame);
            placementFrame = 0;
            coach.hidden = true;
            reserveCoachSpace();
            highlight(null);
        }

        /** 用四张模块卡片作为入口；有进度时分别提供继续与从头开始。 */
        function renderModules() {
            modulesBox.replaceChildren();
            for (const module of TUTORIAL_MODULES) {
                const card = document.createElement('section');
                const title = document.createElement('h3'), description = document.createElement('p');
                const status = document.createElement('span'), actions = document.createElement('div');
                const start = document.createElement('button');
                const saved = progress[module.id];
                const savedIndex = saved ? module.steps.findIndex(step => step.id === saved.stepId) : 0;
                card.className = 'tutorial-module-card';
                title.textContent = module.title;
                description.textContent = module.description;
                status.className = 'tutorial-module-status';
                status.textContent = saved?.completed ? '已完成' : saved ? `上次看到第 ${savedIndex + 1} 步` : `共 ${module.steps.length} 步`;
                actions.className = 'tutorial-module-actions';
                start.type = 'button';
                start.className = 'btn primary';
                start.dataset.tutorialModule = module.id;
                start.textContent = saved?.completed ? '重新学习' : saved ? '继续上次' : '开始引导';
                start.onclick = () => startModule(module.id, Boolean(saved?.completed));
                actions.append(start);
                if (saved && !saved.completed) {
                    const restart = document.createElement('button');
                    restart.type = 'button';
                    restart.className = 'btn';
                    restart.dataset.tutorialRestart = module.id;
                    restart.textContent = '从头开始';
                    restart.onclick = () => startModule(module.id, true);
                    actions.append(restart);
                }
                card.append(title, status, description, actions);
                modulesBox.append(card);
            }
        }

        /** 打开模块选择，不默认进入某项教程。 */
        function openTutorial() {
            if (drawer.hidden && !activeModule) previousFocus = document.activeElement;
            stopGuide();
            renderModules();
            drawer.hidden = false;
            drawer.classList.add('is-open');
            syncExpanded();
            $('closeTutorial').focus({preventScroll: true});
        }

        /** 关闭选择器或引导后，将焦点还给入口或原有可用控件。 */
        function exitTutorial() {
            stopGuide();
            drawer.classList.remove('is-open');
            drawer.hidden = true;
            syncExpanded();
            const focus = previousFocus?.isConnected && !previousFocus.disabled && !previousFocus.closest('[hidden]') ? previousFocus : trigger;
            focus.focus({preventScroll: true});
        }

        /** 根据模块 ID 开始或恢复，重学只重置该模块的阅读状态。 */
        function startModule(id, restart = false) {
            const module = TUTORIAL_MODULES.find(item => item.id === id);
            if (!module || !module.steps.length) return;
            stopGuide();
            activeModule = module;
            const saved = progress[id];
            stepIndex = !restart && saved && !saved.completed ? Math.max(0, module.steps.findIndex(step => step.id === saved.stepId)) : 0;
            complete = false;
            drawer.hidden = true;
            drawer.classList.remove('is-open');
            observer.observe(document.body, {subtree: true, childList: true, attributes: true,
                attributeFilter: ['class', 'hidden', 'disabled', 'aria-expanded', 'aria-hidden']});
            renderStep(true);
            syncExpanded();
            if (!coach.hidden) $('tutorialNext').focus({preventScroll: true});
        }

        /** 仅白名单内的页签、分类和展开按钮允许由教程代为点击。 */
        function navigateStep(step) {
            if (allowedViews.has(step.view)) {
                const tab = document.querySelector(`.tabs .tab[data-view="${step.view}"]`);
                if (tab && !tab.classList.contains('active')) tab.click();
            }
            if (allowedCategories.has(step.category)) {
                const category = $(step.category);
                if (category?.getAttribute('aria-selected') !== 'true') category?.click();
            }
            if (allowedExpanders.has(step.expand)) {
                const disclosure = $(step.expand);
                if (disclosure?.getAttribute('aria-expanded') === 'false') disclosure.click();
            }
        }

        /** 读取正文只使用 textContent，后续步骤不会预先插入卡片。 */
        function renderStep(navigate) {
            if (!activeModule) return;
            const step = activeModule.steps[stepIndex];
            if (navigate) navigateStep(step);
            $('tutorialModuleName').textContent = activeModule.title;
            $('tutorialStepProgress').textContent = complete ? '教程已完成' : `第 ${stepIndex + 1} / ${activeModule.steps.length} 步`;
            $('tutorialStepView').hidden = complete;
            $('tutorialCompleteView').hidden = !complete;
            // 完成视图有独立标题，避免辅助技术仍引用已隐藏的步骤说明。
            coach.setAttribute('aria-labelledby', complete ? 'tutorialCompleteTitle' : 'tutorialStepTitle');
            coach.setAttribute('aria-describedby', complete ? 'tutorialCompleteBody' : 'tutorialStepBody');
            $('tutorialStepActions').hidden = complete;
            $('tutorialPrev').disabled = stepIndex === 0;
            $('tutorialNext').textContent = stepIndex === activeModule.steps.length - 1 ? '完成教程' : '下一步';
            $('tutorialStepTitle').textContent = step.title;
            $('tutorialStepBody').textContent = step.body;
            rememberProgress();
            if (complete) {
                highlight(null);
                sizeObserver?.disconnect();
            }
            refreshPlacement();
            if (navigate && highlighted) {
                highlighted.scrollIntoView({behavior: 'instant', block: window.innerWidth <= 600 ? 'start' : 'center'});
                queuePlacement();
            }
        }

        /** 上一项只改变教程位置，下一项只有明确点击后才推进。 */
        function changeStep(direction) {
            if (!activeModule || complete) return;
            if (direction > 0 && stepIndex === activeModule.steps.length - 1) {
                complete = true;
                renderStep(false);
                $('tutorialRestart').focus({preventScroll: true});
                return;
            }
            stepIndex = Math.max(0, Math.min(activeModule.steps.length - 1, stepIndex + direction));
            renderStep(true);
        }

        /** 判断实际渲染可见性，不能只检查元素自身的 hidden 属性。 */
        function isVisible(element) {
            if (!element?.isConnected || element.closest('[hidden]') || !element.getClientRects().length) return false;
            const style = getComputedStyle(element);
            return style.visibility !== 'hidden' && style.visibility !== 'collapse' && style.display !== 'none';
        }

        /** 动态列表可能暂时为空，找不到可见目标时使用步骤约定的上级区域。 */
        function visibleMatch(selector) {
            if (!selector) return null;
            try { return [...document.querySelectorAll(selector)].find(isVisible) || null; }
            catch { return null; }
        }

        /** 原生开关通常被视觉隐藏，实际高亮应落在用户可点击的标签上。 */
        function anchorFor(element) {
            if (element?.matches('input[type="checkbox"],input[type="radio"]')) {
                return element.closest('label') || element;
            }
            return element;
        }

        /** 非本步骤的业务弹窗优先；教程不抢走弹窗焦点或将其自动关闭。 */
        function modalBlocks(step) {
            const modals = [...document.querySelectorAll('.dialog-backdrop.show,.image-preview.show')].filter(isVisible);
            return modals.some(modal => modal.id !== step?.dialog);
        }

        /** 重新解析动态控件并定位，不推进步骤、不更改业务参数。 */
        function refreshPlacement() {
            if (!activeModule) return;
            const step = activeModule.steps[stepIndex];
            if (modalBlocks(complete ? null : step)) {
                coach.hidden = true;
                reserveCoachSpace();
                highlight(null);
                return;
            }
            coach.hidden = false;
            let target = null, hint = '';
            const onExpectedView = !step.view || $(step.view)?.classList.contains('active');
            $('tutorialReturnStep').hidden = complete || onExpectedView;
            if (!complete && onExpectedView) {
                const actual = visibleMatch(step.target);
                target = anchorFor(actual || visibleMatch(step.fallback) || visibleMatch(`#${step.view} .card`));
                if (!actual) hint = step.missing || '当前条件下此控件尚未显示，可按说明操作后查看，也可以继续下一步。';
                else if (actual.disabled) hint = '当前控件不可操作；可先了解用途，待任务结束或进入编辑状态后再使用。';
            } else if (!complete) hint = '你已切换到其他页面，可点击“返回当前步骤”继续查看。';
            $('tutorialStepHint').textContent = hint;
            $('tutorialStepHint').hidden = !hint;
            if (target !== highlighted) {
                highlight(target);
                sizeObserver?.disconnect();
                sizeObserver?.observe(coach);
                if (target) sizeObserver?.observe(target);
            }
            positionCoach(target);
            reserveCoachSpace(step.dialog ? visibleMatch(`#${step.dialog}`) : null);
        }

        /** 在目标左右或上下选择空位，窗口较窄时使用底部卡片。 */
        function positionCoach(target) {
            const gap = 14, margin = 12, vw = window.innerWidth, vh = window.innerHeight;
            coach.style.maxHeight = `${Math.max(100, (vw <= 600 ? vh * .48 : vh) - 24)}px`;
            if (vw <= 600) {
                coach.style.left = `${margin}px`;
                coach.style.top = 'auto';
                coach.style.bottom = `${margin}px`;
                coach.dataset.side = 'bottom';
                return;
            }
            coach.style.bottom = 'auto';
            const box = coach.getBoundingClientRect();
            let left = vw - box.width - margin, top = margin, side = 'none';
            if (target) {
                const r = target.getBoundingClientRect();
                if (r.right + gap + box.width <= vw - margin) {
                    left = r.right + gap; top = r.top; side = 'right';
                } else if (r.left - gap - box.width >= margin) {
                    left = r.left - gap - box.width; top = r.top; side = 'left';
                } else if (r.bottom + gap + box.height <= vh - margin) {
                    left = r.left; top = r.bottom + gap; side = 'below';
                } else if (r.top - gap - box.height >= margin) {
                    left = r.left; top = r.top - gap - box.height; side = 'above';
                } else { left = r.left; top = vh - box.height - margin; }
            }
            coach.style.left = `${Math.max(margin, Math.min(left, vw - box.width - margin))}px`;
            coach.style.top = `${Math.max(margin, Math.min(top, vh - box.height - margin))}px`;
            coach.dataset.side = side;
        }

        /** 合并滚动、布局和 DOM 更新，避免轮询日志时重复重建引导卡片。 */
        function queuePlacement() {
            if (!activeModule || placementFrame) return;
            placementFrame = requestAnimationFrame(() => {
                placementFrame = 0;
                refreshPlacement();
            });
        }

        const observer = new MutationObserver(records => {
            if (records.some(record => !drawer.contains(record.target) && !coach.contains(record.target))) queuePlacement();
        });
        const sizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(queuePlacement) : null;
        trigger.onclick = openTutorial;
        $('closeTutorial').onclick = exitTutorial;
        $('tutorialExit').onclick = exitTutorial;
        $('tutorialChooseModule').onclick = openTutorial;
        $('tutorialCompleteModules').onclick = openTutorial;
        $('tutorialRestart').onclick = () => { if (activeModule) startModule(activeModule.id, true); };
        $('tutorialPrev').onclick = () => changeStep(-1);
        $('tutorialNext').onclick = () => changeStep(1);
        $('tutorialReturnStep').onclick = () => renderStep(true);
        document.addEventListener('scroll', queuePlacement, true);
        window.addEventListener('resize', queuePlacement);
        document.addEventListener('keydown', event => {
            if (event.key !== 'Escape' || (drawer.hidden && !activeModule)) return;
            if (document.querySelector('.dialog-backdrop.show,.image-preview.show')) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            exitTutorial();
        }, true);
    })();
