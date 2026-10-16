"""Real local Chromium/Flask WebAdmin probes; external connections blocked.
Set OGGY_AUDIT_EVIDENCE_DIR outside checkout to retain screenshots/navigation.
No genuine Telegram/payment/provider call or actual owner-login/2FA rehearsal.
"""
import json,os,shutil,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
import tests.test_phase16_admin_fail_order as fixture
from database import db,reseller_db as rdb
ROOT=Path(__file__).resolve().parents[1]

class WholeProjectBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import web_admin.app as wa
        cls.wa=wa;wa.DB_PATH=fixture._db_path;wa.app.config['TESTING']=False
        cls.server=make_server('127.0.0.1',0,wa.app,threaded=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
        cls.play=sync_playwright().start()
        cls.browser=cls.play.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.play.stop();cls.server.shutdown();cls.thread.join(timeout=5)
    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':1280,'height':900})
        self.addCleanup(self.context.close)
        self.context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(self.base) else route.abort())
        client=fixture.Phase16AdminFailOrderTests()._owner()
        self.context.add_cookies([{'name':'session','value':client.get_cookie('session').value,'url':self.base}])
        self.page=self.context.new_page();self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)))
        self.page.goto(self.base);self.page.locator("#content .loader").wait_for(state="hidden")
    def test_untrusted_customer_name_cannot_execute_balance_handler_code(self):
        uid=8_860_000+fixture._n();attack="');window.__auditInjected=1;//"
        db.ensure_user_id(uid);db.cur.execute('UPDATE users SET first_name=? WHERE user_id=?',(attack,uid));db.conn.commit()
        self.page.evaluate("setView('users')");self.page.locator("#usersSearchBox").wait_for()
        self.page.locator('#usersSearchBox').fill(str(uid));self.page.wait_for_timeout(600)
        self.page.locator('button[onclick^="adjustBalance"]').click()
        self.assertIsNone(self.page.evaluate('window.__auditInjected'))
        self.assertIn(attack,self.page.locator('#modalTitle').inner_text())
        self.assertEqual(self.errors,[])
    def test_product_name_apostrophe_and_unicode_delete_confirmation_is_safe(self):
        name="O'Reilly \"☃\" <test>";db.cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES(?,1,0,'',1)",(name,));pid=db.cur.lastrowid;db.conn.commit()
        self.page.evaluate("setView('products')");self.page.locator('button[onclick^="delProduct"]').first.wait_for()
        self.page.evaluate('window.__deleteArgs=null;window.delProduct=(...args)=>window.__deleteArgs=args')
        self.page.locator(f'button[onclick^="delProduct({pid},"]').click()
        self.assertEqual(self.page.evaluate('window.__deleteArgs'),[pid,name]);self.assertEqual(self.errors,[])
    def test_pricing_button_uses_real_reseller_and_name_and_saves_rule(self):
        uid=8_870_000+fixture._n();db.ensure_user_id(uid);rs=rdb.create_reseller('browser reseller',uid,'testbrowser');rdb.set_reseller_status(rs['id'],'active')
        name="Price's test";db.cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES(?,10,5,'manual',1)",(name,));pid=db.cur.lastrowid;db.conn.commit();rdb.set_product_access(rs['id'],pid,True)
        self.page.evaluate("document.querySelector('#content').innerHTML='<div id=\"resPriceWrap\"></div>'")
        self.page.evaluate('(pub)=>loadResellerPricing(pub)',rs['public_id'])
        self.page.locator(f'button[onclick^="resellerPriceModal"]').filter(has_text='Set price').first.click()
        # Catalog may have older fixture products; target the newly-created row.
        self.page.evaluate('closeModal()')
        target=self.page.locator(f'tr').filter(has_text=name);target.locator('button').first.click()
        self.assertIn(name,self.page.locator('#modalTitle').inner_text())
        self.page.locator('#rp_value').fill('8.50');self.page.locator('#rpSave').click()
        self.page.wait_for_timeout(400)
        value,_=rdb.resolve_reseller_price_cents(rs['id'],pid)
        self.assertEqual(value,850)
    def test_api_application_failure_never_resolves_as_success(self):
        self.page.route('**/api/audit-only-failure',lambda r:r.fulfill(status=200,content_type='application/json',body=json.dumps({'ok':False,'error':'test-only failure'})))
        result=self.page.evaluate("async()=>{try{await post('/api/audit-only-failure',{});return 'resolved'}catch(e){return e.message}}")
        self.assertEqual(result,'test-only failure')
    def test_expired_session_api_rejects_instead_of_resolving_empty_success(self):
        self.page.route('**/api/audit-only-expired',lambda r:r.fulfill(status=401,content_type='application/json',body='{"ok":false,"error":"expired"}'))
        # Prevent test document navigation while observing promise behavior.
        self.page.route('**/login',lambda r:r.abort())
        result=self.page.evaluate("async()=>{try{await api('/api/audit-only-expired');return 'resolved'}catch(e){return e.message}}")
        self.assertNotEqual(result,'resolved')
    def test_all_twenty_views_render_desktop_and_mobile_without_page_errors(self):
        views=self.page.locator('.nav-item').evaluate_all('(els)=>els.map(e=>e.dataset.view)');self.assertEqual(len(views),20)
        records=[]
        for width,height in [(1280,900),(390,844)]:
            self.page.set_viewport_size({'width':width,'height':height})
            for view in views:
                self.page.evaluate('(v)=>setView(v)',view)
                self.page.locator("#content .loader").wait_for(state="hidden")
                body=self.page.locator('#content').inner_text()
                records.append({'view':view,'viewport':[width,height],'failed_load':body.startswith('Failed to load:'),'text_length':len(body)})
            folder=os.environ.get('OGGY_AUDIT_EVIDENCE_DIR')
            if folder:
                p=Path(folder);p.mkdir(parents=True,exist_ok=True)
                self.page.screenshot(path=str(p/f'navigation-{width}.png'),full_page=True)
        self.assertEqual(self.errors,[])
        self.assertEqual([r for r in records if r['failed_load']],[])
        folder=os.environ.get('OGGY_AUDIT_EVIDENCE_DIR')
        if folder:(Path(folder)/'navigation.json').write_text(json.dumps(records,indent=2)+'\n')

if __name__=='__main__':unittest.main()
