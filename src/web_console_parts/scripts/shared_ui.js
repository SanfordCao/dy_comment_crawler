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

    /** 切换高级配置分类，仅切换显示，不重建表单，保留尚未保存的 XPath。 */
    function selectConfigCategory(button) {
        document.querySelectorAll(".config-category").forEach(tab => {
            const selected = tab === button;
            tab.setAttribute("aria-selected", String(selected));
            $(tab.getAttribute("aria-controls")).hidden = !selected;
        });
    }

    document.querySelectorAll(".config-category").forEach(button => {
        button.onclick = () => selectConfigCategory(button);
    });

    function centerSelectArrows() {
        if (CSS.supports("appearance", "base-select")) return;
        const canvas = document.createElement("canvas"), context = canvas.getContext("2d");
        if (!context) return;
        document.querySelectorAll("select").forEach(select => {
            const style = getComputedStyle(select);
            context.font = style.font || `${style.fontSize} ${style.fontFamily}`;
            const text = select.selectedOptions[0]?.textContent || "";
            const width = context.measureText(text.trim()).width;
            select.style.setProperty("--select-text-half", `${Math.min(width, Math.max(0, select.clientWidth - 56)) / 2}px`);
        });
    }

    document.querySelectorAll("select").forEach(select => select.addEventListener("change", centerSelectArrows));
    window.addEventListener("resize", centerSelectArrows);
    document.fonts?.ready.then(centerSelectArrows);

    /** 仅把采集/回复表单的说明收进帮助图标；状态说明仍直接显示。 */
    function initializeOptionHelp() {
        const tooltip = document.createElement('div');
        tooltip.id = 'optionHelpTooltip';
        tooltip.className = 'option-help-tooltip';
        tooltip.setAttribute('role', 'tooltip');
        tooltip.hidden = true;
        document.body.append(tooltip);
        let activeButton = null, closeTimer = null, pinned = false;

        function hide() {
            clearTimeout(closeTimer);
            tooltip.hidden = true;
            activeButton = null;
            pinned = false;
        }

        function show(button) {
            clearTimeout(closeTimer);
            if (activeButton !== button) pinned = false;
            activeButton = button;
            tooltip.textContent = $(button.dataset.helpId).textContent.trim().replace(/\s+/g, ' ');
            tooltip.hidden = false;
            const bounds = button.getBoundingClientRect(), gap = 8, margin = 12;
            const box = tooltip.getBoundingClientRect();
            const left = Math.max(margin, Math.min(bounds.left, innerWidth - box.width - margin));
            const below = bounds.bottom + gap;
            const top = below + box.height <= innerHeight - margin
                ? below : Math.max(margin, bounds.top - gap - box.height);
            tooltip.style.left = `${left}px`;
            tooltip.style.top = `${top}px`;
        }

        function scheduleHide() {
            clearTimeout(closeTimer);
            closeTimer = setTimeout(() => {
                if (!pinned && document.activeElement !== activeButton
                    && !activeButton?.matches(':hover') && !tooltip.matches(':hover')) hide();
            }, 150);
        }

        document.querySelectorAll('#crawl .field, #reply .field').forEach(field => {
            const source = field.querySelector(':scope > small');
            const label = field.querySelector(':scope > label');
            if (!source || !label) return;
            const labelText = label.textContent.trim().replace(/\s+/g, ' ');
            if (!source.id) source.id = `${label.htmlFor}Help`;
            source.hidden = true;
            const row = document.createElement('div');
            row.className = 'field-label-row';
            label.before(row);
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'option-help-button';
            button.textContent = '?';
            button.dataset.helpId = source.id;
            button.setAttribute('aria-label', `查看${labelText}说明`);
            button.setAttribute('aria-describedby', source.id);
            row.append(label, button);
            button.addEventListener('pointerenter', () => show(button));
            button.addEventListener('pointerleave', scheduleHide);
            button.addEventListener('focus', () => show(button));
            button.addEventListener('blur', () => { pinned = false; scheduleHide(); });
            button.addEventListener('click', () => {
                if (activeButton === button && pinned) hide();
                else { show(button); pinned = true; }
            });
        });
        tooltip.addEventListener('pointerenter', () => clearTimeout(closeTimer));
        tooltip.addEventListener('pointerleave', scheduleHide);
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && !tooltip.hidden) {
                hide();
                event.preventDefault();
                event.stopImmediatePropagation();
            }
        }, true);
        document.addEventListener('click', event => {
            if (!event.target.closest('.option-help-button, .option-help-tooltip')) hide();
        });
        document.addEventListener('scroll', event => {
            if (tooltip.contains(event.target)) return;
            // Keyboard focus can scroll its button into view after focus fires.
            if (activeButton && document.activeElement === activeButton) {
                const bounds = activeButton.getBoundingClientRect();
                if (bounds.bottom > 0 && bounds.top < innerHeight) show(activeButton);
                else hide();
            } else hide();
        }, true);
        window.addEventListener('resize', hide);
        document.addEventListener('visibilitychange', hide);
    }

    initializeOptionHelp();
