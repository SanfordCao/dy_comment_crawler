    const $ = id => document.getElementById(id);
    /** 校验重试上限；显式拒绝空值和小数，避免空输入被转换为“不重试”。 */
    function validateRetryLimit(value) {
        if (value === '' || value == null || !Number.isInteger(Number(value)) || Number(value) < 0 || Number(value) > 10) {
            throw new Error('失败重试次数必须是 0～10 的整数');
        }
        return Number(value);
    }

    /** 读取方案编辑框；查看其他方案不会改变实际任务的重试配置。 */
    function readRetryLimit(id) {
        return validateRetryLimit($(id).value);
    }
    let selectedRun = "", selectedRunInfo = null, allComments = [], lastStatus = "";
    let licenseFailure = "";

    /** 授权失效同时显示常驻状态及一次滑出提示，不允许页面再启动采集。 */
    function showLicenseFailure(message) {
        const changed = licenseFailure !== message;
        licenseFailure = message;
        $("statusText").textContent = message;
        $("dot").className = "dot failed";
        $("start").disabled = true;
        $("stop").disabled = true;
        $("waiting").classList.remove("show");
        setStoppingDialog(false);
        if (changed) showNotice(message, true);
    }

    const api = async (url, options = {}) => {
        const response = await fetch(url, options);
        let data;
        try {
            data = await response.json()
        } catch {
            data = null
        }
        if (!response.ok) {
            const error = new Error(data?.error || `请求失败 (${response.status})`);
            error.code = data?.code;
            error.issues = data?.issues;
            error.licenseFailure = response.status === 403 && ["license_expired", "license_invalid"].includes(data?.code);
            if (error.licenseFailure) showLicenseFailure(error.message);
            throw error;
        }
        return data
    };

    /** 采集和回复都用同一键识别重复分享文本，不依赖某个 Tab 的输入控件。 */
    function shareTextKey(value) {
        const text = value.trim();
        if (!text) return "";
        const links = text.match(/https?:\/\/[^\s<>"'，。！？；、（）【】]+/g) || [];
        if (links.length === 1) {
            try {
                const url = new URL(links[0]);
                if (["douyin.com", "iesdouyin.com"].some(domain => url.hostname === domain || url.hostname.endsWith("." + domain))) {
                    const pathId = url.pathname.match(/^\/(?:share\/)?video\/(\d+)\/?$/)?.[1];
                    const ids = new Set([pathId, ...["modal_id", "aweme_id", "item_id"].flatMap(key => url.searchParams.getAll(key))].filter(id => /^\d+$/.test(id || "")));
                    if (ids.size === 1) return "video:" + [...ids][0];
                    if (!ids.size && url.hostname === "v.douyin.com") return "short:" + url.hostname + url.pathname.replace(/\/$/, "") + url.search;
                }
            } catch { /* 非法链接留给启动前完整校验，不在输入过程中猜测视频。 */
            }
        }
        return "text:" + text.replace(/\s+/g, " ");
    }

    /** 统一的滑出提示供全部 Tab、配置页及授权错误复用。 */
    function showNotice(text, error = false) {
        const stack = $("toastStack"), toast = document.createElement("div");
        toast.className = "toast" + (error ? " error" : "");
        toast.setAttribute("role", error ? "alert" : "status");
        const icon = document.createElement("span"), message = document.createElement("span"),
            close = document.createElement("button");
        icon.className = "toast-icon";
        icon.textContent = error ? "!" : "✓";
        icon.setAttribute("aria-hidden", "true");
        message.className = "toast-message";
        message.textContent = text;
        close.className = "toast-close";
        close.type = "button";
        close.textContent = "×";
        close.setAttribute("aria-label", "关闭提示");
        let dismissed = false, timer;
        const dismiss = () => {
            if (dismissed) return;
            dismissed = true;
            clearTimeout(timer);
            toast.classList.add("leaving");
            setTimeout(() => toast.remove(), 220)
        };
        close.onclick = dismiss;
        toast.append(icon, message, close);
        stack.append(toast);
        while (stack.children.length > 4) stack.firstElementChild.remove();
        timer = setTimeout(dismiss, error ? 7000 : 4500);
    }

    document.querySelectorAll(".tab").forEach(button => button.onclick = () => {
        document.querySelectorAll(".tab,.view").forEach(el => el.classList.remove("active"));
        button.classList.add("active");
        $(button.dataset.view).classList.add("active");
        if (button.dataset.view === "history") loadRuns()
        if (button.dataset.view === "reply") pollReplyQueue()
    });
