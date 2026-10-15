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
