    /** 教程仅处理导航和提示，不向业务接口提交操作。 */
    (() => {
        const drawer = $('tutorialDrawer'), trigger = $('openTutorial');
        let previousFocus = null, closeTimer = null, highlightTimer = null;
        let highlighted = null;

        /** 展开抽屉并将焦点放到关闭按钮，支持键盘访问。 */
        function openTutorial() {
            clearTimeout(closeTimer);
            previousFocus = document.activeElement;
            drawer.hidden = false;
            // 先建立初始位置，下一帧再滑入，避免首次打开缺少动画。
            drawer.getBoundingClientRect();
            drawer.classList.add('is-open');
            trigger.setAttribute('aria-expanded', 'true');
            $('closeTutorial').focus();
        }

        /** 关闭后还原焦点；跳转业务区域时由目标自行接管焦点。 */
        function closeTutorial(restoreFocus = true) {
            drawer.classList.remove('is-open');
            trigger.setAttribute('aria-expanded', 'false');
            clearTimeout(closeTimer);
            closeTimer = setTimeout(() => { drawer.hidden = true; }, 210);
            if (restoreFocus && previousFocus?.isConnected) previousFocus.focus();
        }

        /** 切换分类时保留其余内容节点，重置阅读位置。 */
        function selectTopic(button) {
            drawer.querySelectorAll('[data-tutorial-topic]').forEach(item => {
                const active = item === button;
                item.classList.toggle('active', active);
                item.setAttribute('aria-pressed', String(active));
            });
            drawer.querySelectorAll('[data-tutorial-article]').forEach(article => {
                article.hidden = article.dataset.tutorialArticle !== button.dataset.tutorialTopic;
            });
            drawer.querySelector('.tutorial-content').scrollTop = 0;
        }

        /** 显示对应 Tab 和折叠区域，再短暂标记实际需要操作的位置。 */
        function jumpToOperation(button) {
            const tab = document.querySelector(`.tab[data-view="${button.dataset.view}"]`);
            const target = $(button.dataset.target);
            if (!tab || !target) return;
            tab.click();
            if (button.dataset.category) $(button.dataset.category)?.click();
            // 配置可能被用户折叠，先展开才有可见的定位目标。
            const panel = target.closest('.advanced-settings');
            if (panel?.hidden) document.querySelector(`[aria-controls="${panel.id}"]`)?.click();
            closeTutorial(false);
            clearTimeout(highlightTimer);
            highlighted?.classList.remove('tutorial-highlight');
            highlighted = target;
            target.classList.add('tutorial-highlight');
            target.scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'center'});
            const focusTarget = target.matches('input,textarea,button,select') ? target : target.querySelector('input,textarea,button,select');
            // 只移动焦点，不触发目标按钮，避免教程误启动任务。
            if (focusTarget && !focusTarget.disabled) focusTarget.focus({preventScroll: true});
            else trigger.focus({preventScroll: true});
            highlightTimer = setTimeout(() => target.classList.remove('tutorial-highlight'), 3200);
        }

        trigger.onclick = openTutorial;
        $('closeTutorial').onclick = () => closeTutorial();
        drawer.querySelectorAll('[data-tutorial-topic]').forEach(button => {
            button.onclick = () => selectTopic(button);
        });
        drawer.querySelectorAll('.tutorial-jump').forEach(button => {
            button.onclick = () => jumpToOperation(button);
        });
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && drawer.classList.contains('is-open') && !document.querySelector('.dialog-backdrop.show')) {
                closeTutorial();
            }
        });
    })();
