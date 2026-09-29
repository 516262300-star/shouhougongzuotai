import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock
from desktop_app import App
from session_client import SessionError

class ButtonStateTests(unittest.TestCase):
    def app(self):
        app=App.__new__(App)
        for name in ('root','status','name','countdown','total','apply_server','server_entry','login','open','logout'):
            setattr(app,name,Mock())
        app.client=SimpleNamespace(credentials=None,store=SimpleNamespace(description='test'))
        app.change_server=Mock();app.restore_pending=True;app.retry_delay=30;app.retry_at=0;app.kind='restore'
        return app
    def test_restore_network_failure_allows_manual_login(self):
        app=self.app();app.future=Future();app.future.set_exception(SessionError('offline'))
        app.tick()
        self.assertEqual(app.login.config.call_args.kwargs['state'],'normal')
        text=app.status.config.call_args.kwargs['text']
        self.assertNotIn('HTTP', text)
        self.assertNotIn('invalid_token', text)
        self.assertIn('正在检查登录状态', text)

    def test_restore_invalid_token_stays_quietly_logged_out(self):
        app=self.app();app.future=Future()
        app.future.set_exception(SessionError('HTTP 401: invalid_token', invalid=True))
        app.tick()
        self.assertFalse(app.restore_pending)
        text=app.status.config.call_args.kwargs['text']
        self.assertIn('尚未登录', text)
        self.assertIn('点击“登录”', text)
        self.assertNotIn('HTTP', text)
        self.assertNotIn('invalid_token', text)

    def test_restore_configuration_failure_is_visible_and_stays_retryable(self):
        app=self.app();app.future=Future()
        message='服务器认证配置异常。已保留本机登录信息。'
        app.future.set_exception(SessionError(message, configuration=True))
        app.tick()
        self.assertEqual(app.status.config.call_args.kwargs['text'], message)
        self.assertTrue(app.restore_pending)
        self.assertGreater(app.retry_at, 0)
        self.assertEqual(app.login.config.call_args.kwargs['state'], 'normal')

    def test_open_configuration_failure_does_not_ask_for_another_login(self):
        app=self.app();app.future=Future();app.kind='open';app.restore_pending=False
        app.client.credentials=SimpleNamespace(name='测试',user_id=1,expires_at=99999999999,session_expires_at=99999999999,refresh_at=99999999999)
        message='服务器认证配置异常。已保留本机登录信息。'
        app.future.set_exception(SessionError(message, configuration=True))
        app.tick()
        self.assertEqual(app.status.config.call_args.kwargs['text'], message)
        self.assertIsNotNone(app.client.credentials)
    def test_logged_in_button_is_explicit(self):
        app=self.app();app.future=None;app.restore_pending=False
        app.client.credentials=SimpleNamespace(name='测试',user_id=1,expires_at=99999999999,session_expires_at=99999999999,refresh_at=99999999999)
        app.tick()
        self.assertEqual(app.login.config.call_args.kwargs['state'],'normal')
        app.run('login',Mock())
        self.assertIn('已经登录',app.status.config.call_args.kwargs['text'])
    def test_manual_login_stops_restore_retry_and_shows_progress(self):
        app=self.app();app.future=None;app.pool=Mock();action=Mock()
        app.run('login',action)
        self.assertFalse(app.restore_pending)
        self.assertIn('完成首次登录',app.status.config.call_args.kwargs['text'])
        app.pool.submit.assert_called_once_with(action)

    def test_open_without_login_explains_next_step(self):
        app=self.app();app.future=None;app.pool=Mock()
        app.run('open',Mock())
        self.assertIn('尚未登录',app.status.config.call_args.kwargs['text'])
        app.pool.submit.assert_not_called()
    def test_busy_open_click_gives_feedback_without_duplicate(self):
        app=self.app();app.future=Future();app.kind='open';app.pool=Mock()
        app.run('open',Mock())
        self.assertIn('正在打开系统',app.status.config.call_args.kwargs['text'])
        app.pool.submit.assert_not_called()
    def test_open_starts_with_visible_feedback(self):
        app=self.app();app.future=None;app.pool=Mock();app.client.credentials=object();action=Mock()
        app.run('open',action)
        self.assertIn('正在打开系统浏览器',app.status.config.call_args.kwargs['text'])
        app.pool.submit.assert_called_once_with(action)

    def test_empty_restore_tells_user_to_login(self):
        app=self.app();app.future=Future();app.future.set_result(None)
        app.tick()
        text=app.status.config.call_args.kwargs['text']
        self.assertIn('尚未登录',text)
        self.assertIn('点击“登录”',text)
        self.assertNotIn('凭据存储',text)
    def test_restored_session_tells_user_to_open_system(self):
        app=self.app();app.future=Future();app.future.set_result(None)
        app.client.credentials=SimpleNamespace(name='测试',user_id=1,expires_at=99999999999,session_expires_at=99999999999,refresh_at=99999999999)
        app.tick()
        text=app.status.config.call_args.kwargs['text']
        self.assertIn('无需再次登录',text)
        self.assertIn('点击“打开系统”',text)

if __name__=='__main__':unittest.main()
