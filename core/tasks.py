import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
from playwright.sync_api import Response
import time
import json


complates = {}

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
userIDDict = {}


def wait_for_first_selector(page, selectors, timeout=15000):
    """按顺序尝试多个选择器，返回第一个可用的选择器"""
    last_error = None
    for selector in selectors:
        try:
            page.wait_for_selector(selector, timeout=timeout)
            return selector
        except Exception as e:
            last_error = e
    raise RuntimeError(f"所有候选选择器都不可用: {selectors}, 最后错误: {last_error}")


def handle_response(response: Response):
    """
    只监听你要的那个接口响应
    """
    global userIDDict
    if "aweme/v1/creator/im/user_detail/" in response.url:
        try:
            json_data = response.json()
            for item in json_data.get("user_list", []):
                short_id = item.get("user", {}).get("ShortId")
                nickname = item.get("user", {}).get("nickname")
                user_id = item.get("user_id", "")
                userIDDict[str(short_id)] = {"nickname": nickname, "user_id": user_id}
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            last = tb[-1]
            print(f"解析响应失败: {e}")
            print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    """
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise


def scroll_and_select_user(page, username, targets):
    """尝试滚动并查找用户名"""
    friends_tab_selectors = [
        'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]',
        'xpath=//div[contains(normalize-space(.), "好友")]',
        'text=好友',
    ]
    first_friend_selectors = [
        'xpath=//*[@id="sub-app"]/div/div/div[2]/div[2]/div/div/div[1]/div/div/div/ul/div/div/div[1]/li/div',
        'xpath=//ul//li/div',
        'xpath=//div[contains(@class, "semi-list-item-body")]',
    ]
    scrollable_friends_selectors = [
        'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]/div/div/div[3]/div/div/div/ul/div',
        'xpath=//ul/ancestor::div[1]',
    ]

    target_selector = 'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]//div[contains(@class, "semi-list-item-body semi-list-item-body-flex-start")]'
    no_more_selector = 'xpath=//div[contains(@class, "no-more-tip-")]'
    loading_selector = 'xpath=//div[contains(@class, "semi-spin")]'

    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")
    logger.info(f"账号 {username} 当前页面 URL: {page.url}")

    logger.debug(f"账号 {username} 点击进入好友标签页")
    friends_tab_selector = wait_for_first_selector(
        page, friends_tab_selectors, timeout=min(config["browserTimeout"], 30000)
    )

    page.locator(friends_tab_selector).first.click()
    logger.debug(f"账号 {username} 进入好友列表页面")

    first_friend_selector = wait_for_first_selector(
        page, first_friend_selectors, timeout=min(config["browserTimeout"], 30000)
    )

    page.locator(first_friend_selector).first.click()
    logger.debug(f"账号 {username} 已激活好友列表，开始滚动查找目标好友")

    time.sleep(config["friendListTimeout"] / 1000)

    found_targets = set()
    remaining_targets = set(targets)
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10

    while True:
        target_elements = page.locator(target_selector).all()
        prev_found_count = len(found_targets)

        for element in target_elements:
            try:
                span = element.locator(
                    """xpath=.//span[contains(@class, "item-header-name-")]"""
                )
                targetName = span.inner_text()

                if targetName in found_targets:
                    continue
                found_targets.add(targetName)

                logger.debug(f"账号 {username} 找到好友 {targetName}")
                if matchMode == "short_id":
                    targetSymbol = next(
                        (sid for sid, info in userIDDict.items() if info.get("nickname") == targetName),
                        None,
                    )
                else:
                    targetSymbol = targetName

                if targetSymbol in targets:
                    element.click()
                    if matchMode == "short_id":
                        logger.debug(f"账号 {username} 选中目标好友 {targetName} 准备开始交互")
                    else:
                        logger.debug(
                            f"账号 {username} 选中目标好友 {targetName} (ShortId: {targetSymbol}) 准备开始交互"
                        )
                    yield targetName

                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                    break
            except Exception:
                traceback.print_exc()
        else:
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0
            else:
                empty_scroll_count += 1

            if page.locator(no_more_selector).count() > 0:
                logger.info(f"账号 {username} 检测到'没有更多了'标志，已到达底部")
                if len(remaining_targets) > 0:
                    logger.warning(f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}")
                break

            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(
                    f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新好友，判定已到达底部"
                )
                if len(remaining_targets) > 0:
                    logger.warning(f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}")
                break

            if page.locator(loading_selector).count() > 0:
                logger.debug(f"账号 {username} 列表正在加载中 (Loading)...")
                time.sleep(1.5)

            scrollable_element = None
            for selector in scrollable_friends_selectors:
                try:
                    scrollable_element = page.locator(selector).element_handle()
                    if scrollable_element:
                        break
                except Exception:
                    continue

            if scrollable_element:
                scroll_top_before = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )

                page.evaluate(
                    "(element) => element.scrollTop += 800", scrollable_element
                )

                time.sleep(0.3)
                scroll_top_after = page.evaluate(
                    "(element) => element.scrollTop", scrollable_element
                )

                if scroll_top_before == scroll_top_after:
                    empty_scroll_count += 2
                    logger.debug(
                        f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 "
                        f"(空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
                    )
                else:
                    logger.debug(
                        f"账号 {username} 滚动好友列表以加载更多好友 "
                        f"(scrollTop: {scroll_top_before} -> {scroll_top_after})"
                    )

                time.sleep(1.5)
            else:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break


def do_user_task(browser, username, cookies, targets):
    context = browser.new_context()
    context.set_default_navigation_timeout(config["browserTimeout"])
    context.set_default_timeout(config["browserTimeout"])

    page = context.new_page()

    if matchMode == "short_id":
        page.on("response", handle_response)

    try:
        retry_operation(
            "打开抖音创作者中心",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://creator.douyin.com/",
        )

        context.add_cookies(cookies)

        retry_operation(
            "导航到消息页面",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://creator.douyin.com/creator-micro/data/following/chat",
        )

        try:
            page.wait_for_load_state("networkidle", timeout=min(config["browserTimeout"], 20000))
        except Exception:
            logger.warning(f"账号 {username} 等待 networkidle 超时，继续尝试后续流程")

        logger.info(f"账号 {username} 进入消息页后的 URL: {page.url}")
        logger.debug(f"账号 {username} 开始发送消息")

        for target_username in scroll_and_select_user(page, username, targets):
            logger.debug(f"账号 {username} 已选中好友 {target_username} 发送消息")
            chat_input_selector = "xpath=//div[contains(@class, 'chat-input-')]"
            page.wait_for_selector(chat_input_selector, timeout=config["browserTimeout"])
            chat_input = page.locator(chat_input_selector)

            message = build_message()
            for line in message.split("\\n"):
                chat_input.type(line)
                if line != message.split("\\n")[-1]:
                    chat_input.press("Shift+Enter")

            logger.debug(
                f"账号 {username} 准备发送消息给好友 {target_username}：\n\t{message}"
            )
            logger.debug(f"账号 {username} 给好友 {target_username} 发送消息完成")
            chat_input.press("Enter")
            time.sleep(2)

    finally:
        context.close()


def runTasks():
    playwright, browser = get_browser()
    try:
        logger.info("开始执行任务")
        logger.debug("当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}")

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            complates[user["unique_id"]] = []
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            do_user_task(browser, username, cookies, targets)
            logger.info(f"账号 {username} 任务完成")
    finally:
        browser.close()
        playwright.stop()
