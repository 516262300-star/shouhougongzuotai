import base64
import hashlib
import http.client
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from leedis_login import login


class LeedisLoginTests(unittest.TestCase):
    def run_flow(self, invalid=False):
        seen = {}; workers = []; failures = []
        def fetch(url):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(url, timeout=2) as r: return r.status, r.read().decode(), r.headers
            except urllib.error.HTTPError as r: return r.code, r.read().decode(), r.headers
        def open_browser(url):
            seen['url'] = url
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            seen['query'] = q
            def callback():
                try:
                    base = q['redirect_uri'][0]
                    seen['wrong'] = fetch(base + '?' + urllib.parse.urlencode({'state':'wrong', 'code':'c'*43}))
                    good = base + '?' + urllib.parse.urlencode({'state':q['state'][0], 'code':'c'*43})
                    seen['good'] = fetch(good)
                    if invalid:
                        seen['again'] = fetch(good)
                except Exception as e: failures.append(e)
            thread=threading.Thread(target=callback); workers.append(thread); thread.start()
            return True
        def exchange(form):
            seen['form']=form
            if invalid: raise ValueError('invalid server response')
            return {'ok':True}
        try:
            with patch('leedis_login.webbrowser.open', side_effect=open_browser):
                result=login('http://localhost/leedis/index.php/desktopauth', exchange, lambda p,t: None, timeout=2)
                self.assertEqual(result[0], {'ok':True})
        finally:
            for thread in workers: thread.join(3)
            self.assertEqual(failures, [])
            self.seen=seen
        return seen

    def test_localhost_leedis_pkce_callback(self):
        seen=self.run_flow()
        self.assertTrue(seen['url'].startswith('http://localhost/leedis/index.php/desktopauth/authorize?'))
        self.assertNotIn('leedis3',seen['url'])
        challenge=base64.urlsafe_b64encode(hashlib.sha256(seen['form']['code_verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(challenge,seen['query']['code_challenge'][0])
        self.assertEqual(seen['wrong'][0],400)
        self.assertIn('登录成功',seen['good'][1])
        self.assertEqual(seen['good'][2].get('Access-Control-Allow-Origin'), 'http://localhost')
        self.assertIsNone(seen['good'][2].get('Location'))

    def test_exchange_failure_never_shows_success(self):
        with self.assertRaises(ValueError): self.run_flow(invalid=True)
        self.assertNotIn('登录成功',self.seen['good'][1])
        self.assertIn('登录验证失败',self.seen['good'][1])
        self.assertIn('invalid server response',self.seen['good'][1])
        self.assertIsNone(self.seen['good'][2].get('Location'))
        self.assertEqual(self.seen['good'][2].get('Access-Control-Allow-Origin'), 'http://localhost')
        self.assertIn('登录验证失败', self.seen['again'][1])
        self.assertIsNone(self.seen['again'][2].get('Location'))

    def test_login_page_opens_automatically_once_after_valid_state(self):
        opened=[]; failures=[]; workers=[]
        def opener(url):
            opened.append(url)
            if len(opened) > 1: return True
            q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            callback=urllib.parse.urlsplit(q['redirect_uri'][0])
            def worker():
                try:
                    def request(path):
                        conn=http.client.HTTPConnection('127.0.0.1',callback.port,timeout=2)
                        conn.request('GET',path); response=conn.getresponse()
                        result=(response.status,response.getheader('Location'));response.read();conn.close()
                        return result
                    self.assertEqual(request('/login-required?state=wrong')[0],400)
                    for _ in range(2):
                        status,location=request('/login-required?'+urllib.parse.urlencode({'state':q['state'][0]}))
                        self.assertEqual(status,303)
                        self.assertIn('login_opened=1',location)
                        self.assertTrue(location.startswith('http://localhost/leedis/index.php/desktopauth/authorize?'))
                    request('/callback?'+urllib.parse.urlencode({'state':q['state'][0],'code':'c'*43}))
                except Exception as error: failures.append(error)
            thread=threading.Thread(target=worker);workers.append(thread);thread.start()
            return True
        result=login('http://localhost/leedis/index.php/desktopauth',lambda form:{'ok':True},lambda p,t:None,timeout=2,opener=opener)
        for thread in workers:thread.join(3)
        self.assertEqual(failures,[])
        self.assertEqual(len(opened),2)
        self.assertEqual(opened[1],'http://localhost/leedis/index.php/welcome/loginpage')
        self.assertEqual(result[0],{'ok':True})

    def test_timeout(self):
        with self.assertRaises(RuntimeError):
            login('http://localhost/leedis/index.php/desktopauth',lambda p:None,lambda p,t:None,timeout=0.03,opener=lambda url:True)

if __name__ == '__main__': unittest.main()
