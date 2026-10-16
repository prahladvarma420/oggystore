"""Real WebAdmin reseller subtab routes/UI. Temporary SQLite; no external HTTP.
Owner reads are distinct from public reseller API authentication. No mutation UI
or supplier/webhook delivery calls are introduced by this repair.
"""
import json, os, unittest
from pathlib import Path
from utils.crypto import encrypt_text
import tests.test_phase16_admin_fail_order as fixture
import tests.test_whole_project_browser as browser_fixture
from database import db, reseller_db as rdb

def seed():
    n=fixture._n();res=rdb.create_reseller(f'Tab reseller {n}')
    rdb.set_reseller_status(res['id'],'active')
    db.cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES(?,7.25,0,'',1)",(f'Tab product {n}',));pid=db.cur.lastrowid
    ids=[]
    for i in range(3):
        db.cur.execute("INSERT INTO orders(user_id,product_id,product_name,price,source,reseller_id,quantity,unit_price_cents,delivery,master_status) VALUES(0,?,?,7.25,'reseller_api',?,1,725,?,?)",(pid,f'Tab product {n}',res['id'],encrypt_text('test-private-delivery') if i==0 else None,'failed' if i==1 else None));ids.append(db.cur.lastrowid)
    db.conn.commit()
    rdb.log_api_request(res['id'],None,'/api/v1/orders?api_secret=test-private-value','GET',200,request_id=f'tab-request-{n}',response_ms=12,key_id='test-private-key',user_agent='test-private-agent')
    rdb.log_security_event(res['id'],'admin_order_fail',description='test-private-description buyer@example.test',ip='127.0.0.1')
    db.cur.execute("INSERT INTO reseller_webhooks(reseller_id,url,secret_encrypted,enabled) VALUES(?,?,?,1)",(res['id'],'https://example.test/hook/test-private-path?token=test-private-query',encrypt_text('test-private-signing-secret')));db.conn.commit()
    return res,ids

class ResellerTabRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import web_admin.app as wa
        cls.wa=wa;wa.DB_PATH=fixture._db_path;wa.app.config['TESTING']=True
    def setUp(self):
        self.res,self.ids=seed();self.other,self.other_ids=seed()
        self.client=fixture.Phase16AdminFailOrderTests()._owner()
        self.base='/api/resellers/'+self.res['public_id']
    def get(self,section,query=''):return self.client.get(self.base+'/'+section+query)
    def test_orders_scoped_real_status_and_no_delivery_or_buyer_data(self):
        response=self.get('orders');self.assertEqual(response.status_code,200)
        rows=response.get_json()['orders'];self.assertEqual({r['order_id'] for r in rows},{f'ORD-{i:08d}' for i in self.ids})
        self.assertEqual({r['status'] for r in rows},{'completed','failed','processing'});self.assertTrue(all(r['product_name'].startswith('Tab product ') for r in rows))
        text=response.get_data(as_text=True);self.assertNotIn('test-private-delivery',text);self.assertNotIn('buyer_email',text)
    def test_orders_cursor_has_no_omissions_or_duplicates(self):
        first=self.get('orders','?limit=2').get_json();self.assertTrue(first['pagination']['has_more'])
        second=self.get('orders','?limit=2&after_id='+first['pagination']['next_after_id']).get_json()
        self.assertEqual(len(first['orders'])+len(second['orders']),3)
        self.assertEqual(len({r['order_id'] for r in first['orders']+second['orders']}),3)
    def test_api_logs_scoped_and_allowlisted(self):
        response=self.get('api-logs');self.assertEqual(response.status_code,200)
        data=response.get_json();self.assertEqual(len(data['logs']),1);self.assertEqual(data['logs'][0]['endpoint'],'/api/v1/orders')
        for bad in ['test-private-value','test-private-key','test-private-agent','api_key_id','user_agent']:
            self.assertNotIn(bad,response.get_data(as_text=True))
    def test_security_summary_reuses_existing_route_without_free_text(self):
        response=self.get('security-events','?summary=1&limit=2');self.assertEqual(response.status_code,200)
        data=response.get_json();self.assertEqual(len(data['events']),1)
        self.assertEqual(set(data['events'][0]),{'id','event_type','created'})
        self.assertNotIn('test-private-description',response.get_data(as_text=True))
    def test_webhooks_metadata_and_real_analytics_no_url_credentials(self):
        response=self.get('webhooks');self.assertEqual(response.status_code,200)
        data=response.get_json();self.assertEqual(data['webhooks'][0]['destination_origin'],'https://example.test')
        self.assertEqual(data['health'],rdb.get_reseller_webhook_analytics(self.res['id'],days=30))
        for bad in ['test-private-path','test-private-query','test-private-signing-secret','secret_encrypted']:
            self.assertNotIn(bad,response.get_data(as_text=True))
    def test_new_reads_require_login_owner_and_do_not_accept_api_identity(self):
        anon=self.wa.app.test_client();admin=fixture.Phase16AdminFailOrderTests()._owner('admin')
        for section in ['orders','api-logs','webhooks']:
            path=self.base+'/'+section
            self.assertEqual(anon.get(path).status_code,401)
            self.assertEqual(anon.get(path,headers={'Authorization':'Bearer test-only-fake-token'}).status_code,401)
            self.assertEqual(admin.get(path).status_code,403)
    def test_missing_reseller_is_404(self):
        for section in ['orders','api-logs','webhooks','security-events']:
            self.assertEqual(self.client.get('/api/resellers/RES-MISSING/'+section).status_code,404)
    def test_invalid_pagination_and_identity_injection_rejected(self):
        for section in ['api-logs','webhooks','security-events']:
            for query in ['?limit=-1','?limit=0','?limit=abc','?offset=-1','?offset=abc']:
                self.assertEqual(self.get(section,query).status_code,400,(section,query))
        self.assertEqual(self.get('orders','?reseller_id='+str(self.other['id'])).status_code,400)
    def test_metadata_offset_pages_and_empty_results(self):
        rdb.log_api_request(self.res['id'],None,'/api/v1/products','GET',404)
        a=self.get('api-logs','?limit=1').get_json();b=self.get('api-logs','?limit=1&offset=1').get_json()
        self.assertTrue(a['pagination']['has_more']);self.assertFalse(b['pagination']['has_more']);self.assertNotEqual(a['logs'][0]['id'],b['logs'][0]['id'])
        empty=rdb.create_reseller('Empty tab test '+str(fixture._n()))
        for section,key in [('orders','orders'),('api-logs','logs'),('webhooks','webhooks'),('security-events','events')]:
            response=self.client.get('/api/resellers/'+empty['public_id']+'/'+section+'?summary=1' if section=='security-events' else '/api/resellers/'+empty['public_id']+'/'+section)
            self.assertEqual(response.status_code,200);self.assertEqual(response.get_json()[key],[])

class ResellerTabBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):browser_fixture.WholeProjectBrowserTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):browser_fixture.WholeProjectBrowserTests.tearDownClass.__func__(cls)
    def setUp(self):browser_fixture.WholeProjectBrowserTests.setUp(self)
    def open_from_reseller_list(self, res):
        self.page.locator('.nav-item[data-view="resellers"]').click()
        self.page.locator('#resellersSearchBox').wait_for()
        self.page.locator('#resellersSearchBox').fill(res['public_id'])
        self.page.locator('#content tr').filter(has_text=res['public_id']).get_by_role('button',name='Manage',exact=True).click()
        self.page.locator('#resellerTabBody').wait_for()
        self.page.wait_for_timeout(500)  # A stale search debounce/response must not replace the detail.
        self.assertTrue(self.page.locator('#resellerTabBody').is_visible())
    def test_four_tabs_render_actual_seeded_rows_on_desktop_and_mobile(self):
        res,ids=seed();self.open_from_reseller_list(res)
        for width,height in [(1280,900),(390,844)]:
            self.page.set_viewport_size({'width':width,'height':height})
            for key,expected in [('orders',f'ORD-{ids[-1]:08d}'),('apilogs','/api/v1/orders'),('secevents','admin_order_fail'),('webhooks','https://example.test')]:
                self.page.locator('#content').get_by_role('button',name={'orders':'🛒 Orders','apilogs':'🧾 API Logs','secevents':'🛡️ Security Events','webhooks':'🪝 Webhooks'}[key],exact=True).click()
                self.page.locator('#resellerTabBody table').wait_for()
                body=self.page.locator('#resellerTabBody').inner_text();self.assertIn(expected,body)
                self.assertNotIn('later Phase',body);self.assertNotIn('test-private-',body)
                folder=os.environ.get('OGGY_AUDIT_EVIDENCE_DIR')
                if folder:
                    p=Path(folder);p.mkdir(parents=True,exist_ok=True);self.page.screenshot(path=str(p/f'reseller-{key}-{width}.png'),full_page=True)
        self.assertEqual(self.errors,[])
    def test_order_link_opens_existing_filtered_master_order_view(self):
        res,ids=seed();self.open_from_reseller_list(res)
        self.page.locator('#content').get_by_role('button',name='🛒 Orders',exact=True).click()
        self.page.locator('#resellerTabBody table').wait_for();self.page.locator('#resellerTabBody').get_by_role('button',name='Open order',exact=True).first.click()
        self.page.locator('#content .loader').wait_for(state='hidden')
        self.assertEqual(self.page.evaluate('orderFilters'),{'source':'reseller_api','order_id':str(ids[-1])})
        self.assertEqual(self.page.evaluate('currentView'),'orders');self.assertEqual(self.errors,[])
    def test_failed_read_shows_error_and_retry_not_empty_success(self):
        res,_=seed();self.open_from_reseller_list(res)
        self.page.route('**/api/resellers/*/api-logs?*',lambda r:r.fulfill(status=503,content_type='application/json',body=json.dumps({'ok':False,'error':'Temporary test outage'})))
        self.page.get_by_role('button',name='🧾 API Logs',exact=True).click()
        self.page.locator('#resellerTabBody .res-detail-error').wait_for();self.assertIn('Temporary test outage',self.page.locator('#resellerTabBody').inner_text())
        self.assertTrue(self.page.locator('#resellerTabBody').get_by_role('button',name='Retry',exact=True).is_visible());self.assertEqual(self.errors,[])

if __name__=='__main__':unittest.main()
