import unittest
from unittest.mock import patch
from PIL import Image
from updater.disaster.fsm import DisasterUpdater, GUIProfile


def hit(text, box=(10, 10, 110, 40)):
    return (box, text, .99)


class LauncherGateTests(unittest.TestCase):
    def setUp(self):
        self.up = DisasterUpdater(GUIProfile("test", "test.exe", ["test"], []), poll_interval=0)
        self.up.p.launcher_quiet_seconds = 0
        process = patch("updater.disaster.fsm.window.process_running", return_value=False)
        process.start()
        self.addCleanup(process.stop)
        for name, value in [("find_updater_windows", []), ("find_window", "window")]:
            mocker = patch("updater.disaster.fsm.window." + name, return_value=value)
            mocker.start()
            self.addCleanup(mocker.stop)
        self.shot = Image.new("RGB", (1000, 800))

    def detect(self, context=None, cancel=None, update=None, busy=None, action=None):
        return patch.object(self.up, "_find_text", side_effect=[None, context, cancel, update, busy, action])

    @patch("updater.disaster.fsm.inp.click")
    @patch("updater.disaster.fsm.window.to_screen", side_effect=lambda win, x, y: (x, y))
    def test_context_confirm_offsets_and_background_done(self, screen, click):
        with self.detect(hit("启动器更新"), hit("取消"), None, None, hit("确定")):
            self.assertTrue(self.up._handle_launcher_update(None, self.shot))
        click.assert_called_once_with(240, 161)
        self.assertTrue(self.up._launcher_pending)

    @patch("updater.disaster.fsm.inp.click")
    def test_cancel_alone_blocks_without_click(self, click):
        with self.detect(None, hit("取消")):
            self.assertTrue(self.up._handle_launcher_update(None, self.shot))
        click.assert_not_called()

    @patch("updater.disaster.fsm.inp.click")
    def test_no_dialog_does_not_block(self, click):
        with self.detect():
            self.assertFalse(self.up._handle_launcher_update(None, self.shot))
        click.assert_not_called()

    @patch("updater.disaster.fsm.inp.click")
    def test_busy_blocks_without_click(self, click):
        with self.detect(hit("启动器更新"), None, None, hit("正在下载")):
            self.assertTrue(self.up._handle_launcher_update(None, self.shot))
        click.assert_not_called()

    def test_click_cap_is_explicit_failure(self):
        self.up._launcher_clicks = self.up.p.launcher_click_cap
        with self.detect(hit("启动器更新"), None, None, None, hit("确定")):
            with self.assertRaisesRegex(RuntimeError, "上限"):
                self.up._handle_launcher_update(None, self.shot)

    @patch("updater.disaster.fsm.inp.click")
    @patch("updater.disaster.fsm.window.to_screen", side_effect=lambda win, x, y: (x, y))
    def test_generic_update_requires_cancel(self, screen, click):
        with self.detect(None, hit("取消"), hit("更新"), None, hit("更新")):
            self.assertTrue(self.up._handle_launcher_update(None, self.shot))
        click.assert_called_once()

    @patch("updater.disaster.fsm.inp.click")
    def test_retry_has_cooldown(self, click):
        import time
        self.up._launcher_last_click = time.monotonic()
        with self.detect(hit("启动器更新"), None, None, None, hit("确定")):
            self.assertTrue(self.up._handle_launcher_update(None, self.shot))
        click.assert_not_called()

    def test_game_success_waits_for_dialog_and_stable_rounds(self):
        def find(shot, words, **kw):
            return hit("开始游戏") if "开始游戏" in words else None
        with patch.object(self.up, "_kill_launcher_processes"), patch("updater.disaster.fsm.subprocess.Popen"), patch("updater.disaster.fsm.time.sleep"), patch("updater.disaster.fsm.window.find_window", return_value="win"), patch("updater.disaster.fsm.window.screenshot", return_value=self.shot), patch.object(self.up, "_wait_launcher_ready", return_value="win"), patch.object(self.up, "_ensure_game_page", return_value=True), patch.object(self.up, "_handle_launcher_update", side_effect=[True, False, False, False]) as gate, patch.object(self.up, "_find_button", return_value=None), patch.object(self.up, "_find_text", side_effect=find), patch.object(self.up, "_finish", return_value=True) as finish:
            self.assertTrue(self.up.run(timeout=10))
            self.assertEqual(gate.call_count, 4)
            finish.assert_called_once_with("win", True)

    def test_process_must_disappear_for_full_five_minutes(self):
        self.up.p.launcher_quiet_seconds = 300
        with patch("updater.disaster.fsm.time.monotonic", side_effect=[0, 299, 300, 301, 302, 601, 602]), patch("updater.disaster.fsm.window.process_running", side_effect=[False, False, False, True, False, False, False]):
            self.assertEqual([self.up._launcher_process_settled() for _ in range(7)],
                             [False, False, True, False, False, False, True])

    @patch("updater.disaster.fsm.inp.click")
    @patch("updater.disaster.fsm.window.to_screen", side_effect=lambda win, x, y: (x, y))
    def test_repeated_confirm_and_optional_finish_are_intermediate(self, screen, click):
        self.up.p.launcher_click_interval = 0
        for word in ["确定", "确定", "完成"]:
            self.up._launcher_absent_since = 123
            with patch.object(self.up, "_find_text", return_value=hit(word)):
                self.assertTrue(self.up._handle_launcher_update(None, self.shot))
            self.assertTrue(self.up._launcher_pending)
            self.assertIsNone(self.up._launcher_absent_since)
        self.assertEqual(click.call_count, 3)

    def test_independent_updater_finish_then_auto_restart(self):
        self.up.p.launcher_quiet_seconds = 300
        with patch("updater.disaster.fsm.window.find_updater_windows", side_effect=[["updater"], [], [], []]), patch("updater.disaster.fsm.window.screenshot", return_value=self.shot), patch.object(self.up, "_launcher_process_settled", side_effect=[False, False, False, True]), patch.object(self.up, "_find_text", side_effect=[hit("完成"), hit("开始游戏"), hit("开始游戏"), hit("开始游戏")]), patch.object(self.up, "_handle_launcher_update", return_value=False), patch.object(self.up, "_click_launcher_action") as click, patch("updater.disaster.fsm.subprocess.Popen") as launch:
            self.assertEqual(self.up._wait_launcher_ready(None, float("inf")), "window")
            self.assertEqual(click.call_args.args[0], "updater")
            launch.assert_not_called()

    def test_missing_launcher_restarts_only_after_quiet_and_rechecks(self):
        with patch("updater.disaster.fsm.window.find_window", side_effect=[RuntimeError("missing"), RuntimeError("missing"), "new", "new", "new", "new"]), patch("updater.disaster.fsm.window.screenshot", return_value=self.shot), patch.object(self.up, "_launcher_process_settled", side_effect=[False, True, False, False, False, True]) as settled, patch.object(self.up, "_find_text", return_value=hit("开始游戏")), patch.object(self.up, "_handle_launcher_update", return_value=False), patch("updater.disaster.fsm.subprocess.Popen") as launch:
            self.assertEqual(self.up._wait_launcher_ready(None, float("inf")), "new")
            launch.assert_called_once()
            self.assertEqual(settled.call_count, 6)

    def test_restart_cap_prevents_endless_relaunch(self):
        self.up.p.launcher_restart_cap = 1
        with patch("updater.disaster.fsm.window.find_window", side_effect=RuntimeError("missing")), patch.object(self.up, "_launcher_process_settled", return_value=True), patch("updater.disaster.fsm.subprocess.Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "多次恢复"):
                self.up._wait_launcher_ready(None, float("inf"))
            launch.assert_called_once()

    @patch("updater.disaster.fsm.window.screenshot")
    def test_ready_requires_consecutive_visual_rounds(self, screenshot):
        screenshot.return_value = self.shot
        with patch.object(self.up, "_handle_launcher_update", side_effect=[False, True, False, False, False]), patch.object(self.up, "_find_text", return_value=hit("开始游戏")):
            self.assertEqual(self.up._wait_launcher_ready("window", float("inf")), "window")
        self.assertEqual(screenshot.call_count, 5)

    @patch("updater.disaster.fsm.window.screenshot")
    @patch("updater.disaster.fsm.window.find_window", return_value="new")
    def test_restart_reacquires_window(self, find, screenshot):
        screenshot.side_effect = [RuntimeError("gone"), self.shot, self.shot, self.shot]
        with patch.object(self.up, "_handle_launcher_update", return_value=False), patch.object(self.up, "_find_text", return_value=hit("开始游戏")), patch("updater.disaster.fsm.window.process_running", return_value=False):
            self.assertEqual(self.up._wait_launcher_ready("old", float("inf")), "new")


if __name__ == "__main__":
    unittest.main()
