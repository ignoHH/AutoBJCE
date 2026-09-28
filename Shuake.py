import threading
from enum import Enum

from playwright.async_api import async_playwright
from getcourseid import Get_course_id
import asyncio
import random
import time


# ── 固定的专题入口（需求：必修 / 选修 两条链接写死） ────────────────────────
MANDATORY_URL = "https://bjce.bjdj.gov.cn/#/course/courseResources?activedIndex=1&id=zhengzhililun"
MANDATORY_CHANNEL = "zhengzhililun"
OPTIONAL_URL = "https://bjce.bjdj.gov.cn/#/course/courseResources?activedIndex=4&id=zonghesuzhi"
OPTIONAL_CHANNEL = "zonghesuzhi"
HOME_URL = "https://bjce.bjdj.gov.cn/#/"

# 网络波动自愈等待时长（秒）
RECOVERY_WAIT_SEC = 60
# 登录阶段等待时长（毫秒）
LOGIN_TIMEOUT_MS = 60000


class NoRemainingCourseError(Exception):
    """当前专题页面已无未完成课程。"""


class Shuake:
    def __init__(
        self,
        user: dict,
        mandatory_target: float,
        optional_target: float,
        log_cb=None,
        progress_cb=None,
    ):
        """
        user: {"name": str, "username": str, "password": str}
        mandatory_target: 目标必修学时（<=0 表示不刷必修）
        optional_target: 目标选修学时（<=0 表示不刷选修）
        log_cb: 日志回调函数 log_cb(str)，不传则 fallback 到 print
        progress_cb: 学时进度回调 progress_cb(mandatory: float, optional: float)
        """
        self.page = None
        self.context = None
        self.browser = None
        self.user = user
        self.mandatory_target = float(mandatory_target or 0)
        self.optional_target = float(optional_target or 0)
        self._log = log_cb if log_cb else print
        self._progress_cb = progress_cb
        self._stop_event = threading.Event()

    @property
    def _stop(self) -> bool:
        return self._stop_event.is_set()

    def stop(self):
        self._stop_event.set()

    def log(self, msg: str):
        self._log(msg)

    # ── 主流程 ────────────────────────────────────────────────────────────────
    async def start(self):
        playwright = await async_playwright().start()
        try:
            self.browser = await playwright.chromium.launch(
                channel='msedge', headless=False, args=['--mute-audio']
            )
            self.context = await self.browser.new_context()
            self.page = await self.context.new_page()
            await self._goto_with_retry(HOME_URL)
            await self.login()
            await self._wait_login_ready(timeout_ms=LOGIN_TIMEOUT_MS)
            await self._main_loop()
        finally:
            try:
                if self.browser:
                    await self.browser.close()
            except Exception as e1:
                self.log(f"错误信息{e1}")
            try:
                await playwright.stop()
            except Exception as e2:
                self.log(f"错误信息{e2}")
            # 关闭共享 aiohttp session，避免资源泄漏
            from getcourseid import close_session
            await close_session()


    async def _main_loop(self):
        """
                循环学习
                """

        class StudyType(Enum):
            """
            宏定义必修和选修两个类别常量
            """
            mandatory = "必修"
            optional = "选修"

        """按目标学时在必修/选修之间自动切换，直到两类均达标或用户停止。"""
        current_kind = None  # 记录上一次刷的类型，尽量避免来回跳转
        while not self._stop:
            try:
                m, o = await self.check_user_core()
            except Exception as e:
                await self._recover_after_error(e)
                continue

            need_m = self.mandatory_target > 0 and m < self.mandatory_target
            need_o = self.optional_target > 0 and o < self.optional_target
            if not need_m and not need_o:
                self.log("必修 / 选修均已达标，任务完成。")
                return

            # 优先保持上一次的类型（避免频繁切页）
            if current_kind == "必修" and need_m:
                kind = "必修"
            elif current_kind == "选修" and need_o:
                kind = "选修"
            elif need_m:
                kind = "必修"
            else:
                kind = "选修"

            if kind != current_kind:
                self.log("=" * 30 + f" ▶ 切换到 {kind} 继续学习 " + "=" * 30)
                current_kind = kind

            url, cid = (MANDATORY_URL, MANDATORY_CHANNEL) if kind == "必修" else (OPTIONAL_URL, OPTIONAL_CHANNEL)
            progress_now = m if kind == "必修" else o
            target_now = self.mandatory_target if kind == "必修" else self.optional_target
            self.log(f"当前目标：{kind} {progress_now} / {target_now} 学时")

            try:
                await self._run_one_course(url, cid)
            except NoRemainingCourseError:
                # 当前专题页面没有未完成课；若对应类型仍未达标，说明该专题本身课程不足
                self.log(f"专题「{kind}」下已无剩余未完成课程。")
                if kind == "必修" and need_m:
                    # 若仍需必修但必修专题没课了，尝试切到选修；若也达标则结束
                    if need_o:
                        current_kind = "选修"
                        continue
                    self.log("必修专题课程已枯竭，但目标仍未达成；请手动确认后重试。")
                    return
                if kind == "选修" and need_o:
                    if need_m:
                        current_kind = "必修"
                        continue
                    self.log("选修专题课程已枯竭，但目标仍未达成；请手动确认后重试。")
                    return
            except Exception as e:
                await self._recover_after_error(e)

        if self._stop:
            self.log("已手动停止刷课。")

    # ── 登录 / 页面就绪 ───────────────────────────────────────────────────────
    async def _wait_login_ready(self, timeout_ms: int = 180000):
        start = time.time()
        while (time.time() - start) * 1000 < timeout_ms:
            # 已登录后首页会出现学时区域
            score_block = await self.page.query_selector("div.iv-row-left-bottom-div2")
            if score_block:
                return

            # 验证码场景：提示用户手动完成
            captcha_input = await self.page.query_selector('input[placeholder*="验证码"]')
            captcha_img = await self.page.query_selector("img[src*='captcha'], img[alt*='验证码']")
            if captcha_input or captcha_img:
                self.log("检测到验证码，请在浏览器中手动完成验证码后继续。")

            await asyncio.sleep(2)

        raise TimeoutError(f"登录后页面未就绪，等待超时({timeout_ms}ms)")

    async def _goto_with_retry(self, url: str):
        try:
            await self.page.goto(url, timeout=90000, wait_until="domcontentloaded")
            return
        except Exception:
            pass
        await self.page.goto(url, timeout=90000, wait_until="domcontentloaded")

    async def login(self):
        """
        登录
        Returns:

        """
        selected_user = self.user
        self.log(f"正在登录用户：{selected_user['name']}")

        try:
            await self.page.wait_for_load_state('domcontentloaded')

            login_button = await self.page.wait_for_selector('//span[text()="请登录"]', timeout=LOGIN_TIMEOUT_MS)
            await login_button.click()

            await self.page.wait_for_load_state('domcontentloaded')

            login_option = await self.page.wait_for_selector('//div[text()="登录"]', timeout=LOGIN_TIMEOUT_MS)
            await login_option.click()

            username_input = await self.page.wait_for_selector('[placeholder="请输入账号"]', timeout=LOGIN_TIMEOUT_MS)
            if username_input:
                await username_input.fill(selected_user["username"])
            else:
                self.log("用户名输入框未找到，请检查选择器。")
                return

            password_input = await self.page.wait_for_selector('[placeholder="请输入密码"]', timeout=LOGIN_TIMEOUT_MS)
            if password_input:
                await password_input.fill(selected_user["password"])
            else:
                self.log("密码输入框未找到，请检查选择器。")
                return

            wxlogin_button = await self.page.wait_for_selector('//span[text()="微信认证登录"]', timeout=LOGIN_TIMEOUT_MS)
            await wxlogin_button.click()
            self.log(f"用户 {selected_user['name']} 登录成功！")

        except Exception as e:
            self.log(f"登录过程中发生错误：{e}")
            raise

    # ── 学时查询 ──────────────────────────────────────────────────────────────
    async def check_user_core(self) -> tuple[float, float]:
        """读取首页必修/选修已学学时，返回 (mandatory, optional)。"""
        # 为了每次都能读到最新值，强制回到首页
        await self._goto_with_retry("https://bjce.bjdj.gov.cn/#/")
        await self.page.wait_for_load_state('load')

        async def _read(selector: str) -> float:
            div = await self.page.wait_for_selector(selector, timeout=LOGIN_TIMEOUT_MS)
            el = await div.query_selector('span[style="font-size: 40px; font-weight: 600;"]')
            if not el:
                return 0.0
            text = (await el.inner_text()).strip()
            try:
                return float(text)
            except ValueError:
                return 0.0

        mandatory = await _read('div.iv-row-left-bottom-div2')
        optional = await _read('div.iv-row-left-bottom-div3')
        self.log(f"已学学时：必修 {mandatory} / 选修 {optional}")
        if self._progress_cb:
            self._progress_cb(mandatory, optional)
        return mandatory, optional


    async def _get_course_link(self, url: str, channel_id: str):
        """
        刷课（每次只刷一门后返回，由外层决定切换）
        Args:
            url:
            channel_id:

        Returns:
            未完成的课程
        """
        await self._goto_with_retry(url)
        cookies = await self.context.cookies()
        cookies = '; '.join([f"{cookie['name']}={cookie['value']}" for cookie in cookies])

        # 等待课程列表容器出现即可，不必先数 DOM
        try:
            container = await self.page.wait_for_selector('div.iv-template-every > ul', timeout=30000)
            course_items = await container.query_selector_all('li[data-v-d50a91fc]')
            row_length = len(course_items)
            return await Get_course_id(cookies, channel_id, page_size=row_length, current_page=1)
        except Exception as e:
            self.log(f"错误信息{e}.")


    async def _run_one_course(self, url: str, channel_id: str):
        """在指定专题页面下刷一门未完成课程；若无剩余课程则抛 NoRemainingCourseError。"""
        uncompleted_courses = await self._get_course_link(url, channel_id)
        self.log(f"本链接剩余课程 {len(uncompleted_courses)} 门")

        if not uncompleted_courses:
            raise NoRemainingCourseError()

        current_course = uncompleted_courses[0]
        course_name = current_course.get("name")
        progress = current_course.get("progress") or 0
        set_type = current_course.get("setType")

        self.log(f"尝试进入课程：《{course_name}》，进度：{progress}")
        success = await self._simulate_click_to_play_course(course_name, set_type)
        if not success:
            self.log(f"课程《{course_name}》进入失败，跳过。")
            await self._close_and_return_to_main_window()
            return
        self.log(f"成功完成课程：《{course_name}》")

    async def _simulate_click_to_play_course(self, course_name, set_type):
        try:
            await self.page.wait_for_load_state('domcontentloaded')
            await asyncio.sleep(2)

            course_item = await self.page.query_selector(f"div.iv-zhezhao-courseName:text-is('{course_name}')")
            if not course_item:
                self.log(f"未找到课程《{course_name}》，请检查 HTML 结构。")
                return False

            li_element = await course_item.evaluate_handle("el => el.closest('li')")
            await li_element.hover()
            await asyncio.sleep(1)

            buttons = await li_element.query_selector_all("span:text-is('开始学习'), span:text-is('继续学习')")

            for button in buttons:
                if await button.is_visible():
                    await button.click()
                    break
            previous_url = self.page.url
            await asyncio.sleep(2)
            all_pages = self.context.pages

            # 检查是否打开新页面
            # 未打开新页面
            if len(all_pages) < 2:
                if self.page.url == previous_url:
                    self.log("检测到页面 URL 变化，当前仍在原窗口")
                else:
                    self.log("未检测到新窗口或 URL 变化，请检查页面逻辑")
            else:
                new_page = all_pages[-1]
                await new_page.wait_for_load_state()
                self.page = new_page
                # 1.检查是否存在#iv-aicc-iframe
                iframe_tag = await self.page.locator('#iv-aicc-iframe').count() # 大于0则说明存在
                # 2.存在#iv-aicc-iframe
                if iframe_tag > 0:
                    frame = self.page.frame_locator('#iv-aicc-iframe')
                    await frame.get_by_role('button', name='继续学习').click()
                self.log(f"已切换到新窗口，开始学习《{course_name}》")
            # elif self.page.url != previous_url:
            #     self.log("检测到页面 URL 变化，当前仍在原窗口")
            # else:
            #     self.log("未检测到新窗口或 URL 变化，请检查页面逻辑")

            if set_type == 1: # 课程没分节
                await self._monitor_course_progress(course_name)
            elif set_type == 3: #课程分节
                await self._play_series_sections()

            await self._close_and_return_to_main_window()
            return True

        except Exception as e:
            self.log(f"进入课程《{course_name}》时发生错误：{e}")
            await self._close_and_return_to_main_window()
            # 让外层 _recover_after_error 统一处理网络级异常
            raise

    async def _close_and_return_to_main_window(self):
        all_pages = self.context.pages
        if len(all_pages) > 1:
            try:
                await self.page.close()
            except Exception:
                pass
            await asyncio.sleep(1)
            self.page = all_pages[0]
            self.log("已切换回主课程页面窗口。")
            self.log("=" * 44 + " 下一节课 " + "=" * 44)
        else:
            self.log("未检测到新窗口，无需切换。")

    async def _fetch_course_sections(self):
        await self.page.wait_for_load_state('domcontentloaded')

        sections = await self.page.query_selector_all("ul.iv-course-play-detail-menu-item > li")
        completed_sections = []
        uncompleted_sections = []

        for section in sections:
            progress_element = await section.query_selector("div.ivu-chart-circle-inner span")
            progress_text = await progress_element.inner_text() if progress_element else "0%"
            progress = int(progress_text.strip("%")) if progress_text.strip("%").isdigit() else 0

            title_element = await section.query_selector("p.iv-course-play-detail-menu-title")
            title = await title_element.inner_text() if title_element else "未知标题"

            section_data = {"title": title, "progress": progress, "element": section}
            if progress >= 100:
                completed_sections.append(section_data)
            else:
                uncompleted_sections.append(section_data)

        return completed_sections, uncompleted_sections

    async def _play_series_sections(self):
        await self.page.wait_for_load_state('domcontentloaded')
        _, uncompleted_sections = await self._fetch_course_sections()

        while uncompleted_sections and not self._stop:
            section = uncompleted_sections.pop(0)
            title = section["title"]
            await section["element"].click()
            await asyncio.sleep(6)
            self.log(f"开始学习小节：{title}")
            try:
                await self._monitor_course_progress(title)
            except Exception as e:
                self.log(f"小节《{title}》异常：{e}")

        if self._stop:
            self.log("系列课程学习被手动停止。")
            return

        self.log("系列课程学习完成！")
        await self._close_and_return_to_main_window()


    async def _monitor_course_progress(self, course_name):
        self.log("监控播放状态中...")

        TOTAL_TIMEOUT = 3600 # 单节课最长播放时间
        start_time = time.monotonic()
        POLL_INTERVAL = 5  # 轮询间隔（秒）
        last_activity = start_time
        ACTIVITY_INTERVAL = 1500 # 多久模拟一次鼠标活动

        last_pause_click = 0.0
        PAUSE_CLICK_COOLDOWN = 15

        while not self._stop:

            now = time.monotonic()

            if now - start_time >= TOTAL_TIMEOUT:
                self.log(f"《{course_name}》播放超时，返回主界面。")
                await self._close_and_return_to_main_window()
                return

            # 1.检查是否存在#iv-aicc-iframe
            iframe_tag = await self.page.locator('#iv-aicc-iframe').count()  # 大于0则说明存在
            # 2.存在#iv-aicc-iframe
            if iframe_tag > 0:
                video = self.page.frame_locator("iframe").locator("video")
                state = await video.evaluate(
                    """(v) => {
                        if (!v) return null;
                        return {
                            paused: v.paused,
                            duration: v.duration || 0,
                            current: v.currentTime || 0,
                        };
                    }"""
                )
            else:
                # 一次 evaluate 拿到全部视频状态，避免多次跨进程调用
                state = await self.page.evaluate(
                    """() => {
                        const v = document.querySelector('video');
                        if (!v) return null;
                        return {
                            paused: v.paused,
                            duration: v.duration || 0,
                            current: v.currentTime || 0,
                        };
                    }"""
                )

            if state is None:
                self.log("未检测到视频元素，等待中...")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            duration = state["duration"] # 完整时长
            current = state["current"] # 学习时长
            progress = (current / duration) if duration > 0 else 0

            # 播放完成（两种判定，任一成立即可）
            if progress >= 0.85 or (duration > 0 and current >= duration - 1):
                self.log(f"《{course_name}》播放完成。")
                return

            # 暂停 → 恢复播放，加冷却避免疯狂点
            # todo iframe
            if state["paused"] and now - last_pause_click >= PAUSE_CLICK_COOLDOWN:
                self.log("检测到视频暂停，尝试恢复播放。")
                try:
                    if iframe_tag:
                        frame = self.page.frame_locator("iframe")
                        btn = await frame.locater("button.jp-play")
                        if btn and await btn.is_visible():
                            await btn.click()
                            last_pause_click = now
                    else:
                        btn = await self.page.query_selector("xg-start.xgplayer-start")
                        if btn and await btn.is_visible():
                            await btn.click()
                            last_pause_click = now
                        else:
                            btn = await self.page.query_selector("xg-icon.xgplayer-play")
                            if btn and await btn.is_visible():
                                await btn.click()
                                last_pause_click = now
                except Exception as e:
                    self.log(f"错误信息{e}")
                    await self._recover_after_error(e)

            # 模拟用户活动
            if now - last_activity >= ACTIVITY_INTERVAL:
                await self._simulate_user_activity()
                last_activity = now

            await asyncio.sleep(POLL_INTERVAL)

        # 走到这里说明 _stop 被触发
        self.log(f"《{course_name}》监控被手动停止。")


    async def _simulate_user_activity(self):
        activity_type = random.choice(["mouse_move", "scroll"])

        if activity_type == "mouse_move":
            x, y = random.randint(10, 50), random.randint(10, 50)
            await self.page.mouse.move(x, y)
            self.log(f"模拟鼠标移动到 ({x}, {y})")
        elif activity_type == "scroll":
            scroll_distance = random.randint(-10, 10)
            await self.page.evaluate(f"window.scrollBy(0, {scroll_distance})")
            self.log(f"模拟页面滚动：滚动距离 {scroll_distance}")

    # ── 网络自愈 ──────────────────────────────────────────────────────────────
    async def _recover_after_error(self, e: Exception):
        self.log(f"遇到异常：{e}")
        self.log(f"将于 {RECOVERY_WAIT_SEC} 秒后刷新页面重试。")

        # 每 30 秒打印一次倒计时，期间响应 _stop
        waited = 0
        while waited < RECOVERY_WAIT_SEC:
            if self._stop:
                return
            await asyncio.sleep(1)
            waited += 1
            if waited % 30 == 0 and waited < RECOVERY_WAIT_SEC:
                self.log(f"恢复倒计时：还剩 {RECOVERY_WAIT_SEC - waited} 秒...")

        # 确保只剩一个主窗口（课程播放窗口可能已崩溃）
        try:
            pages = self.context.pages
            if len(pages) > 1:
                for p in pages[1:]:
                    try:
                        await p.close()
                    except Exception:
                        pass
                self.page = pages[0]
        except Exception:
            pass

        try:
            await self.page.reload(timeout=90000, wait_until="domcontentloaded")
            self.log("页面已刷新，继续学习。")
        except Exception:
            try:
                await self._goto_with_retry("https://bjce.bjdj.gov.cn/#/")
                self.log("已重新打开首页，继续学习。")
            except Exception as e2:
                self.log(f"恢复时仍失败：{e2}，将在下轮继续尝试。")
