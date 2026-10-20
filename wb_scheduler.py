"""wb_scheduler.py —— 后台定时调度器 (Scheduler)

负责常驻后台自动执行：
1. Token 保活 (Keepalive)：定期检查 Token 剩余寿命，不足 2 小时自动调用 Refresh Token。
2. 每日签到 (Daily Checkin)：每日定时为所有国内版账号自动签到领积分。
3. 对话活跃上报 (Activity Report)：每日定时为国内版账号上报轻量会话事件，点亮连登与热力墙。
4. 猫猫旅行与日常结算 (Cat Travel & Welfare)：自动派出猫猫旅行或领取归来奖励。
5. 状态持久化与看板展示：暴露状态、执行记录、支持手动立即触发与开关切换。
"""
import threading
import time
import random
import wb_tasks
import wb_quota_scan
import wb_settings
from wb_tasks import do_cat_travel


class Scheduler:
    def __init__(self, pool):
        self.pool = pool
        # 默认排程对齐 Sliverkiss/workbuddy2api (CST 24小时制)；实际值从
        # settings.json 的 schedule 段读取，缺省 / 非法回落这些默认值。
        self.checkin_hours = [9, 21]     # 每日 09:00、21:00 签到
        self.travel_hours = [9, 21]      # 每日 09:00 派出、21:00 领奖闭环
        self.keepalive_hours = [22]      # 每日 22:00 集中 Token 保活检查
        self.cat_hours = [1]             # 每日 01:00 夜猫子专属任务
        self.all_hours = sorted(list(set(self.checkin_hours + self.travel_hours + self.keepalive_hours + self.cat_hours)))
        self.enabled = True
        self._stop_event = threading.Event()
        self._thread = None
        self._probe_thread = None
        self._scan_thread = None
        self.last_run_time = None
        self.next_run_time = None
        self.logs = []
        # Guards against overlapping runs: trigger_now() spawns a thread per
        # click, and a manual trigger can also land on top of the hourly job.
        self._run_lock = threading.Lock()
        self._reload_schedule()
        self._calc_next_fire()
        # Surface task-level failures (dead endpoints, upstream shape changes)
        # in the same log the panel shows.
        wb_tasks.set_logger(self.log)

    def log(self, msg, tag=None):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        try:
            import wb_proxy
            # 任务/打卡/旅行类条目同时进入 tasks 模块日志，方便分类查看
            effective_tag = tag
            if not effective_tag:
                lower = str(msg).lower()
                if any(k in lower for k in ("签到", "打卡", "活跃", "猫猫", "连登", "夜猫", "抽奖", "兑换")):
                    effective_tag = "tasks"
                else:
                    effective_tag = "scheduler"
            wb_proxy.add_log_entry(f"[调度器] {msg}", tag=effective_tag)
        except Exception:
            pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动")

    def stop(self):
        self._stop_event.set()
        self.log("后台定时调度器已暂停")

    def _run_loop(self):
        # 启动后先休眠 10 秒等待主服务就绪，然后执行初次检查
        time.sleep(10)
        try:
            self._execute_cycle("启动初次初始化巡检")
        except Exception as exc:
            self.log(f"初次巡检异常: {exc}")
        # 进程停过一段时间的话，重置时刻可能已经过去：启动就补一次窗口探针，
        # 把新一轮窗口的起点尽量抢回来。
        self._run_window_probes()

        while not self._stop_event.is_set():
            self._reload_schedule()
            self._calc_next_fire()
            # 每 30 秒检查一次当前整点
            now = time.localtime()
            cur_hour = now.tm_hour
            cur_min = now.tm_min
            if self.enabled:
                # 到达设定的整点前 1 分钟内触发
                if cur_min == 0 and cur_hour in self.all_hours:
                    reason = f"整点排程命中 ({cur_hour}:00)"
                    try:
                        self._execute_cycle(reason)
                    except Exception as exc:
                        self.log(f"排程执行异常: {exc}")
                    time.sleep(65) # 避开当前这一分钟重复触发
            # 模型额度窗口探针：不受 enabled 影响。窗口是按首次请求起算的，
            # 到点没人请求就不会开始新一轮，这件事跟签到/保活开关无关。
            self._run_window_probes()
            # 模型额度定时主动查询：同样不受 enabled 影响——它是「哪些配额线
            # 已经打满」的唯一主动信息源，跟签到/保活不是一回事。
            self._run_quota_scans()
            self._stop_event.wait(self._next_wait())

    def _run_window_probes(self):
        """在独立线程里执行到点的窗口探针。

        一次探针最长可能等上游几十秒，跑在巡检循环里会把整点排程的节奏带偏；
        wb_window_probe 自带防重入锁，重叠启动是无害的。
        """
        if self._probe_thread and self._probe_thread.is_alive():
            return
        self._probe_thread = threading.Thread(target=self._probe_worker, daemon=True)
        self._probe_thread.start()

    def _probe_worker(self):
        try:
            import wb_window_probe
            wb_window_probe.run_due(self.pool, self.log)
        except Exception as exc:
            self.log(f"窗口探针异常: {exc}")

    def _run_quota_scans(self):
        """到点的模型额度主动查询，同样跑在独立线程里。

        一次查询要等上游几十秒，塞进巡检循环会把整点排程的节奏带偏；
        wb_quota_scan 自带防重入锁，重叠启动是无害的。
        """
        if self._scan_thread and self._scan_thread.is_alive():
            return
        self._scan_thread = threading.Thread(target=self._scan_worker, daemon=True)
        self._scan_thread.start()

    def _scan_worker(self):
        try:
            import wb_quota_scan
            wb_quota_scan.run_due(self.pool, self.log)
        except Exception as exc:
            self.log(f"额度查询异常: {exc}")

    def _next_wait(self):
        """下一轮巡检的等待时间：常规 30 秒，有窗口即将到点时提前醒来。

        探针的价值在于「到点立刻发」——晚一个巡检周期，新一轮窗口的起点就
        往后推 30 秒，所以这里按最近的窗口到期时间收敛等待。
        """
        wait = 30.0
        try:
            import wb_window_probe
            accounts_dir = getattr(self.pool, "dir", "") or ""
            secs = wb_window_probe.seconds_until_due(accounts_dir)
            if secs is not None:
                wait = min(wait, max(1.0, secs))
        except Exception:
            pass
        return wait

    def _reload_schedule(self):
        """Re-read the configurable hours; keep the current grid on failure.

        wb_settings.load() is cached by (path, mtime, size), so calling this
        every loop tick is a stat, not a parse. A changed grid recomputes the
        next fire time immediately, which is what makes an edit take effect
        without restarting the gateway.
        """
        directory = getattr(self.pool, "dir", "") or ""
        if not directory:
            return False
        try:
            hours = wb_settings.schedule_hours(directory)
        except Exception:
            return False
        changed = (hours["checkin"] != self.checkin_hours
                   or hours["travel"] != self.travel_hours
                   or hours["keepalive"] != self.keepalive_hours
                   or hours["cat"] != self.cat_hours)
        self.checkin_hours = hours["checkin"]
        self.travel_hours = hours["travel"]
        self.keepalive_hours = hours["keepalive"]
        self.cat_hours = hours["cat"]
        self.all_hours = sorted(set(self.checkin_hours + self.travel_hours
                                    + self.keepalive_hours + self.cat_hours))
        if changed:
            self._calc_next_fire()
        return changed

    def _calc_next_fire(self):
        now = time.localtime()
        cur_h = now.tm_hour
        next_h = None
        for h in self.all_hours:
            if h > cur_h or (h == cur_h and now.tm_min == 0 and now.tm_sec < 10):
                next_h = h
                break
        if next_h is not None:
            # 今天
            t_struct = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, next_h, 0, 0, 0, 0, -1))
        else:
            # 明天第一个小时
            t_tomorrow = time.time() + 86400
            now_tom = time.localtime(t_tomorrow)
            first_h = self.all_hours[0]
            t_struct = time.struct_time((now_tom.tm_year, now_tom.tm_mon, now_tom.tm_mday, first_h, 0, 0, 0, 0, -1))
        self.next_run_time = time.strftime("%Y-%m-%d %H:%M:%S", t_struct)

    def trigger_now(self):
        """手动立即触发一次调度检查。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(target=self._execute_cycle, args=("手动立即触发",), daemon=True).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检"):
        if not self._run_lock.acquire(blocking=False):
            self.log(f"跳过本次巡检 ({trigger_reason})：上一轮仍在执行")
            return
        try:
            self._run_cycle(trigger_reason)
        finally:
            self._run_lock.release()

    def _run_cycle(self, trigger_reason="周期巡检"):
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log(f"开始执行任务 ({trigger_reason})...")
        if not self.pool or not self.pool.accounts:
            self.log("暂无可用的活跃账号，跳过本次巡检")
            return

        refreshed_count = 0
        checkin_count = 0
        report_count = 0
        travel_count = 0
        daily_chat_count = 0
        trae_checkin_count = 0

        try:
            import wb_settings
            trae_auto = wb_settings.trae_auto_checkin(self.pool.dir)
        except Exception:
            trae_auto = True

        for idx, acc in enumerate(list(self.pool.accounts)):
            if idx > 0:
                # 账号间随机错峰防并发风控（2~5秒浮动延时，issue #394）
                time.sleep(random.uniform(2.0, 5.0))
            uid8 = acc.uid[:8] if acc.uid else "?"
            # 上游账号级风控限制（403 / code 11140）中的账号一律跳过：无人值守
            # 的巡检/打卡再去「试探」一次，只会让同一台设备上的其他账号连坐。
            # 限制期限是持久化的（wb_accounts.note_restricted），重启后仍然有效。
            if getattr(acc, "restricted_until", 0.0) > time.time():
                self.log(f"跳过受限账号 [{uid8}]：上游风控限制剩余 "
                         f"{int(acc.restricted_until - time.time())}s")
                continue
            # 区域级连坐暂停期间（同一窗口内多个账号被限制）整轮都不碰上游。
            try:
                import wb_proxy
                hold = wb_proxy.restriction_hold_remaining(acc.realm)
            except Exception:
                hold = 0.0
            if hold > 0:
                self.log(f"跳过 [{uid8}]：{acc.realm} 区域风控暂停剩余 {int(hold)}s")
                continue
            # 1. 检查 Token 剩余寿命 (小于 2 小时自动刷新保活)
            exp = acc.expires_at or 0
            if exp and (exp - time.time()) < 7200:
                self.log(f"账号 [{uid8}] Token 即将到期，执行主动保活刷新...")
                if acc.refresh():
                    refreshed_count += 1
                    self.log(f"✓ 账号 [{uid8}] Token 自动保活刷新成功")
                else:
                    self.log(f"! 账号 [{uid8}] Token 保活刷新失败: {acc.last_error}")

            # 2. Trae 国内版：每日签到（150+50 积分）。国际版接口未证实，
            #    这里只跑国内版；自动签到可在「任务与福利」页关闭。
            if acc.provider == "trae" and acc.realm == "cn" and acc.enabled:
                if trae_auto and acc.can_checkin():
                    self.log(f"检测到 Trae 国内版账号 [{uid8}] 今日尚未签到，执行自动签到...")
                    res = acc.checkin(trigger="scheduler")
                    if res.get("ok"):
                        trae_checkin_count += 1
                        self.log(f"✓ Trae 账号 [{uid8}] 自动签到成功: {res.get('msg')}")
                    else:
                        self.log(f"! Trae 账号 [{uid8}] 自动签到未成功: {res.get('error') or res.get('msg')}")
                    time.sleep(1.0)
                elif trae_auto:
                    # 已签到也偶尔刷一次积分余额（每 6 小时最多一次），
                    # 免得看板上的积分停在上次签到的数字。
                    try:
                        fetched = float(acc.trae_auth.get("creditsAt") or 0)
                    except (TypeError, ValueError):
                        fetched = 0.0
                    if time.time() - fetched > 6 * 3600:
                        try:
                            acc._fetch_credits_trae()
                        except Exception:
                            pass

            # 3. 如果是国内版 WorkBuddy 账号，检查每日签到与猫猫旅行
            #    （Qoder 没有签到/成长任务；保活刷新仍然对所有提供商生效）
            if acc.realm == "cn" and acc.provider == "workbuddy":
                if acc.can_checkin():
                    self.log(f"检测到国内版账号 [{uid8}] 今日尚未签到，执行自动签到...")
                    res = acc.checkin(trigger="scheduler")
                    if res.get("ok"):
                        checkin_count += 1
                        self.log(f"✓ 账号 [{uid8}] 自动签到成功: {res.get('msg')}")
                    else:
                        self.log(f"! 账号 [{uid8}] 自动签到未成功: {res.get('error') or res.get('msg')}")
                    time.sleep(1.0)

                if acc.can_report_activity():
                    self.log(f"检测到国内版账号 [{uid8}] 今日尚未活跃上报，执行对话活跃上报...")
                    res = acc.report_activity()
                    if res.get("ok"):
                        report_count += 1
                        self.log(f"✓ 账号 [{uid8}] {res.get('msg')}")
                    else:
                        self.log(f"! 账号 [{uid8}] 对话活跃上报失败: {res.get('error') or res.get('msg')}")
                    time.sleep(1.0)

                # 检查猫猫旅行
                tr = do_cat_travel(acc)
                if tr.get("action") in ("claim", "depart"):
                    travel_count += 1
                    self.log(f"🐱 账号 [{uid8}] 猫猫日常处理: {tr.get('msg')}")
                time.sleep(1.0)

                # 连登管家闭环（补签保连登 -> 礼包/补偿 -> 连登兑换 -> 自动抽奖）
                bonus = wb_tasks.run_streak_bonus(acc)
                if bonus.get("logs"):
                    for l in bonus["logs"]:
                        self.log(f"★ 账号 [{uid8}] {l}")
                    time.sleep(1.0)

                # 01:00 夜猫子专属任务: black_cat 只在 23:00-08:00 上报计数,
                # 之前这个整点只是空转通用巡检, 从未真正上报过夜猫事件。
                if time.localtime().tm_hour in self.cat_hours:
                    night = wb_tasks.run_night_growth(acc)
                    for line in night.get("logs", []):
                        self.log(f"🌙 {line}")
                    time.sleep(1.0)

            # 3. 如果是国际版 WorkBuddy 账号，检查每日活跃对话 (送 30/50 积分福利)
            if acc.realm == "intl" and acc.provider == "workbuddy":
                if acc.can_daily_chat():
                    self.log(f"检测到国际版账号 [{uid8}] 今日尚未活跃，执行每日活跃打卡对话...")
                    res = acc.daily_chat(trigger="scheduler")
                    if res.get("ok"):
                        daily_chat_count += 1
                        # 成功时也把网页通道那一轮的结果带上（completed：N 段输出），
                        # 光看「成功」分不清是真跑完了还是只有桌面腿过了（issue #236）。
                        self.log(f"✓ 账号 [{uid8}] 每日活跃对话成功: {res.get('msg')}")
                    else:
                        self.log(f"! 账号 [{uid8}] 每日活跃对话失败: {res.get('error') or res.get('msg')}")
                    time.sleep(1.5)

        self.log(f"巡检完成：Token保活 {refreshed_count} 个，国内签到 {checkin_count} 个，Trae签到 {trae_checkin_count} 个，活跃上报 {report_count} 个，猫猫日常 {travel_count} 个，国际活跃 {daily_chat_count} 个")

    def status(self):
        # 待触发的模型额度窗口：面板上要能看见「下一次到点是什么时候」，
        # 否则自动请求是个黑盒。读不到就当作没有，不影响状态接口本身。
        windows = []
        try:
            import wb_window_probe
            accounts_dir = getattr(self.pool, "dir", "") or ""
            windows = wb_window_probe.snapshot(accounts_dir)[:10]
        except Exception:
            windows = []
        # 主动查询队列：同样要让 periodicity 可见（下一次查哪个模型、还有多久）。
        scans = []
        try:
            import wb_quota_scan
            accounts_dir = getattr(self.pool, "dir", "") or ""
            scans = wb_quota_scan.snapshot(accounts_dir, limit=6)
        except Exception:
            scans = []
        self._reload_schedule()

        def _hhmm(hours):
            return "/".join("%02d:00" % h for h in hours)

        checkin = _hhmm(self.checkin_hours)
        travel = _hhmm(self.travel_hours)
        keepalive = _hhmm(self.keepalive_hours)
        cat = _hhmm(self.cat_hours)
        mode_cn = ("整点排程 (签到 %s · 猫猫旅行 %s · 每日活跃上报 · Trae 每日签到 · "
                   "Token保活 %s · 夜猫 %s) + 模型额度窗口探针"
                   % (checkin, travel, keepalive, cat))
        mode_intl = ("账号 Token 自动保活与凭证常驻 (每日 %s 集中巡检) "
                     "+ 模型额度窗口探针" % keepalive)
        return {
            "enabled": self.enabled,
            "mode": mode_cn,
            "mode_cn": mode_cn,
            "mode_intl": mode_intl,
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "windows": windows,
            "quota_scans": scans,
            "logs": self.logs[-20:],
        }
