    /** 回显同一全局配置文件中的按钮 XPath 和视频信息接口路径。 */
    async function loadXpathSettings() {
        try {
            const settings = await api("/api/settings");
            $("pauseXpath").value = settings.pause_xpath || "";
            $("authorApiPath").value = settings.author_api_path || "";
            $("authorJsonPath").value = settings.author_json_path || "";
            $("titleApiPath").value = settings.title_api_path || "";
            $("titleJsonPath").value = settings.title_json_path || "";
            $("privateChatXpath").value = settings.private_chat_xpath || "";
            $("sendMessageXpath").value = settings.send_message_xpath || "";
            $("closeChatPanelXpath").value = settings.close_chat_panel_xpath || ""
        } catch (error) {
            showXpathNotice(error.message, true)
        }
    }

    function showXpathNotice(message, error = false) {
        showNotice(message, error)
    }

    /** 两个分类共用一次全局保存，防止保存某一类时丢掉另一类配置。 */
    async function saveGlobalSettings() {
        try {
            await api("/api/settings", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    pause_xpath: $("pauseXpath").value,
                    author_api_path: $("authorApiPath").value,
                    author_json_path: $("authorJsonPath").value,
                    title_api_path: $("titleApiPath").value,
                    title_json_path: $("titleJsonPath").value,
                    private_chat_xpath: $("privateChatXpath").value,
                    send_message_xpath: $("sendMessageXpath").value,
                    close_chat_panel_xpath: $("closeChatPanelXpath").value
                })
            });
            showXpathNotice("全局配置已保存，新采集任务和私信操作将使用此配置")
        } catch (error) {
            showXpathNotice(error.message, true)
        }
    }

    $("saveXpath").onclick = saveGlobalSettings;
    $("saveApiSettings").onclick = saveGlobalSettings;
    $("clearApiSettings").onclick = () => {
        for (const id of ["authorApiPath", "authorJsonPath", "titleApiPath", "titleJsonPath"]) $(id).value = "";
    };
    $("clearXpath").onclick = () => {
        $("pauseXpath").value = "";
        $("privateChatXpath").value = "";
        $("sendMessageXpath").value = "";
        $("closeChatPanelXpath").value = ""
    };
    $("advancedToggle").onclick = () => {
        const panel = $("advancedSettings"), expanded = panel.hidden;
        panel.hidden = !expanded;
        $("advancedToggle").setAttribute("aria-expanded", String(expanded));
        $("advancedToggleLabel").textContent = expanded ? "收起　⌃" : "展开　⌄"
    };
    $("historyAdvancedToggle").onclick = () => {
        const panel = $("historyAdvancedPanel");
        panel.hidden = !panel.hidden;
        $("historyAdvancedToggle").setAttribute("aria-expanded", String(!panel.hidden))
    };
