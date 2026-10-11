"""打开用户私信，并仅在用户显式点击发送后提交一条消息。"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

# placeholder 属于空输入框的提示状态，填写后可能消失；定位必须使用稳定的编辑器属性。
CHAT_EDITOR = '.messageEditorinputArea[contenteditable="true"]'
CHAT_HEADER = ".StackLayoutStackChatHeadertitle"
CHAT_SEC_UID = """element => {
    let fiber = element[Object.keys(element).find(key => key.startsWith('__reactFiber'))];
    for (let depth = 0; fiber && depth < 24; depth++, fiber = fiber.return) {
        const conversation = fiber.memoizedProps?.curConversation;
        if (conversation?.toParticipantSecUserId) return String(conversation.toParticipantSecUserId);
    }
    return null;
}"""


async def current_chat_sec_uid(page: Any) -> str | None:
    """从实测会话组件读取收件人标识；内部页面结构变化时不猜测身份。"""
    header = page.locator(CHAT_HEADER)
    if await header.count() != 1 or not await header.is_visible():
        return None
    value = await header.evaluate(CHAT_SEC_UID)
    return value if isinstance(value, str) else None


def validated_message(value: Any) -> str:
    """限制为用户输入的一条非空文本，避免无内容提交及超长消息。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请先填写私信内容")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) > 1000:
        raise ValueError("私信内容不能超过 1000 个字符")
    return value


def normalized_editor_text(value: str) -> str:
    """去除富文本编辑器零宽占位符及首尾空白，不改变正文或 emoji 的连接字符。"""
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\u200b", "").replace("\ufeff", "").strip()


def validated_sec_uid(value: Any) -> str:
    """只接受已有评论中的 sec_uid 标识，拒绝 URL、空值及路径注入。"""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,256}", value):
        raise ValueError("该评论缺少有效 sec_uid，无法确认私聊对象")
    return value


def is_profile_response(response: Any, sec_uid: str) -> bool:
    """先检查域名、接口路径和目标标识，再读取对应用户资料响应。"""
    parsed = urlparse(response.url)
    return (parsed.hostname == "www.douyin.com"
            and parsed.path.rstrip("/") == "/aweme/v1/web/user/profile/other"
            and parse_qs(parsed.query).get("sec_user_id") == [sec_uid])


async def check_chat_blockers(page: Any) -> None:
    """识别可见登录、验证和故障提示，停止流程而不自动重试或绕过限制。"""
    for pattern, message in [
        (r"请先登录|登录后(才能|可)(发|查看|使用)|登录后发送", "请在采集浏览器登录自己的账号，再手动重试"),
        (r"请完成验证|安全验证|拖动滑块|验证码", "页面需要验证，请在浏览器中人工处理后重试"),
        (r"页面崩溃|页面加载失败|该页面无法显示|该用户不存在", "用户页面加载失败，请在浏览器中检查后重试"),
        (r"无法发送私信|不能发送私信|不允许.*私信|仅.*互关.*私信", "该用户当前无法私信，请检查对方隐私设置或关系限制"),
    ]:
        if await page.get_by_text(re.compile(pattern)).first.is_visible():
            raise RuntimeError(message)


async def open_user_chat(context: Any, author: dict[str, Any],
                         on_ready: Callable[[Any, str], None] | None = None,
                         private_chat_xpath: str = "") -> dict[str, Any]:
    """以 sec_uid 核对主页身份并确认私信界面出现，不以昵称作为身份依据。"""
    sec_uid = validated_sec_uid(author.get("sec_uid"))
    # 缺少明确入口配置时提前停止，不打开主页或猜测其他私信按钮。
    if not isinstance(private_chat_xpath, str) or not private_chat_xpath.strip():
        raise RuntimeError("请先在高级配置的 XPath配置中填写并保存私信按钮 XPath；未打开主页或发送消息")
    page = await context.new_page()
    page.set_default_timeout(10000)
    profile_url = f"https://www.douyin.com/user/{quote(sec_uid, safe='')}"
    popups = []

    def on_popup(popup: Any) -> None:
        """记录新打开的私信标签页，供后续核对聊天界面使用。"""
        popups.append(popup)

    # Playwright 会为回调添加映射属性；内置 list.append 不支持属性赋值，必须包装成普通函数。
    page.on("popup", on_popup)
    try:
        await page.bring_to_front()
        # 没有经过实测的直达私信 URL 不拼接；使用站点自己提供的入口作为安全回退。
        async with page.expect_response(lambda response: is_profile_response(response, sec_uid), timeout=15000) as pending:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            await check_chat_blockers(page)
        response = await pending.value
        result = await response.json()
        user = result.get("user") if isinstance(result, dict) else None
        parsed = urlparse(page.url)
        if (not isinstance(result, dict) or result.get("status_code") not in {None, 0}
                or not isinstance(user, dict) or user.get("sec_uid") != sec_uid
                or parsed.hostname != "www.douyin.com" or parsed.path.rstrip("/") != f"/user/{sec_uid}"):
            raise RuntimeError("未能确认用户身份，已停止私信操作；请核对打开的主页")
        await check_chat_blockers(page)
        # 私密账号虽然展示私信按钮，但实测可能没有聊天界面，不能将点击视为成功。
        if await page.get_by_text("私密账号", exact=True).first.is_visible():
            raise RuntimeError("该账号为私密账号，请在抖音中确认是否可私信；未发送消息")
        # 只使用管理员显式填写的 XPath，失效时不得自动退回其他入口。
        buttons = page.locator(f"xpath={private_chat_xpath.strip()}")
        await buttons.first.wait_for(state="visible", timeout=10000)
        visible = [button for button in await buttons.all() if await button.is_visible()]
        if len(visible) != 1:
            raise RuntimeError("私信入口不唯一，已停止操作，请在主页手动选择")
        if not await visible[0].is_enabled():
            raise RuntimeError("该用户的私信入口不可用，请检查隐私或关系限制")
        await visible[0].click(timeout=10000)
        await page.wait_for_timeout(1000)
        target = popups[-1] if popups else page
        await target.bring_to_front()
        await check_chat_blockers(target)
        # 输入框和会话标题只用于确认界面打开；昵称会变化，不适合作为账号身份依据。
        editor = target.locator(CHAT_EDITOR)
        await editor.wait_for(state="visible", timeout=10000)
        header = target.locator(CHAT_HEADER)
        await header.wait_for(state="visible", timeout=10000)
        nickname = str(user.get("nickname") or author.get("nickname") or "匿名用户")
        # 打开入口仍可人工使用；只有当前聊天 sec_uid 已确认时才允许控制台直接发送。
        send_ready = await current_chat_sec_uid(target) == sec_uid
        if send_ready and on_ready is not None:
            on_ready(target, sec_uid)
        return {"status": "entry_opened", "sec_uid": sec_uid,
                "nickname": nickname, "send_ready": send_ready,
                "message": ("已通过 sec_uid 核对主页与聊天对象，可填写内容并点击发送私信。"
                            if send_ready else "私信界面已打开，但无法确认聊天对象的 sec_uid；请在抖音中核对并手动发送。")}
    except PlaywrightTimeout as error:
        raise RuntimeError("用户资料或私信入口未及时加载。主页已保留，请检查登录、验证、网络或是否有私信权限；不会自动重试") from error
    except PlaywrightError as error:
        raise RuntimeError("浏览器或用户页面异常。请检查页面并手动重试；未发送消息") from error
    finally:
        # 注册和移除使用同一函数对象，确保失败时也不会遗留事件监听器。
        page.remove_listener("popup", on_popup)
        # 保留独立标签页供用户登录、核对和手动发送，不关闭或导航采集中的页面。


async def send_user_message(page: Any, sec_uid: str, message: str) -> dict[str, Any]:
    """对核验过的单个会话填入文本并点击发送一次，任何失败均不自动重试。"""
    sec_uid = validated_sec_uid(sec_uid)
    message = validated_message(message)
    clicked = False
    try:
        if page.is_closed():
            raise RuntimeError("私信标签页已关闭，请重新打开私信")
        await page.bring_to_front()
        await check_chat_blockers(page)
        if await current_chat_sec_uid(page) != sec_uid:
            raise RuntimeError("聊天会话已切换或无法核对 sec_uid，未发送；请重新打开目标私信")
        editor = page.locator(CHAT_EDITOR)
        await editor.wait_for(state="visible", timeout=10000)
        # 不覆盖用户已经在抖音界面编辑的草稿，防止发送混合内容。
        if normalized_editor_text(await editor.inner_text()):
            raise RuntimeError("抖音输入框已有草稿，请先人工清理后重新打开私信")
        # Slate 富文本框可能重建 fill() 修改的 DOM；使用原生文本输入，让编辑器同步内部状态。
        await editor.click(timeout=10000)
        await page.keyboard.insert_text(message)
        expected_text = normalized_editor_text(message)
        actual_text = ""
        for attempt in range(6):
            actual_text = normalized_editor_text(await editor.inner_text())
            if actual_text == expected_text:
                break
            # 这里只等待编辑器渲染，不再次输入、更不补发，避免重复内容。
            if attempt < 5:
                await page.wait_for_timeout(200)
        else:
            raise RuntimeError(f"输入框内容与待发送文本不一致（期望 {len(expected_text)} 字符，实际 {len(actual_text)} 字符），未发送；请检查抖音草稿")
        # 填入期间用户可能切换会话，提交前必须再次校验标识。
        if await current_chat_sec_uid(page) != sec_uid:
            raise RuntimeError("聊天会话已切换，未发送；请人工检查输入框草稿")
        button = editor.locator('xpath=ancestor::div[contains(concat(" ", normalize-space(@class), " "), " messageMsgInputinputColumn ")]').locator(".e2e-send-msg-btn")
        if await button.count() != 1 or not await button.is_visible() or not await button.is_enabled():
            raise RuntimeError("发送控件不可用，未发送；请检查抖音私信权限")
        # 开始点击后即可能已提交，即使超时也不能安全补发。
        clicked = True
        await button.click(timeout=10000)
        await page.wait_for_timeout(1000)
        await check_chat_blockers(page)
        # 只有编辑器已清空才关闭本次私信页；仍有文字时保留页面，方便核对发送结果。
        for attempt in range(6):
            if not normalized_editor_text(await editor.inner_text()):
                break
            if attempt < 5:
                await page.wait_for_timeout(200)
        else:
            return {"status": "submitted", "page_closed": False,
                    "message": "已点击发送，但输入框未清空，暂未确认提交结果；私信标签页已保留，请人工核对，勿重复发送。"}
        await check_chat_blockers(page)
        try:
            # 只关闭当前私信独立页，不关闭采集页面或共享浏览器。
            # Page.close 没有 timeout 参数；用 asyncio 限定清理时间，不能把清理失败当发送失败。
            await asyncio.wait_for(page.close(), timeout=5)
        except (PlaywrightError, TimeoutError):
            return {"status": "submitted", "page_closed": False,
                    "message": "发送操作已提交，但私信标签页未能自动关闭，请手动关闭；不会重复发送。"}
        return {"status": "submitted", "page_closed": True,
                "message": "发送操作已提交，私信标签页已自动关闭；是否送达以抖音实际记录为准。"}
    except (PlaywrightError, RuntimeError) as error:
        prefix = "发送结果不确定，请到抖音核对，勿直接重复发送。" if clicked else "未点击发送按钮。"
        raise RuntimeError(f"{prefix}{error}") from error
