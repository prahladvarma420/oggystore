"""17.3: authoritative API parity, pure renderers, fresh access and >100 rows.

Only middleware persistence/rate infrastructure and Telegram sends are isolated.
Actual catalog/pricing services and API routes read a test-owned SQLite database.
"""
import os,asyncio,json,re,unittest,threading,hashlib,ast
os.environ['DATABASE_URL']='';os.environ['BOT_TOKEN']=''
from unittest.mock import patch,AsyncMock
from types import SimpleNamespace
from pathlib import Path
import tests.test_phase16_admin_fail_order as fixtures
from handlers import reseller_handlers as h
from reseller_api.services.product_import import read_product_mappings_for_reseller
from reseller_api.services.pricing import read_product_prices_for_reseller,PricingError
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.context import ApiRequestContext
from reseller_api.services import product_import,pricing

db,rdb=fixtures.db,fixtures.rdb
_counter=0
class TelegramCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):db.init_db();fixtures.Phase16AdminFailOrderTests.setUpClass()
    @classmethod
    def tearDownClass(cls):fixtures.Phase16AdminFailOrderTests.tearDownClass()
    def setUp(self):
        global _counter
        _counter+=1;self.uid=99973000+_counter;db.ensure_user_id(self.uid)
        self.rs=rdb.create_reseller('Catalog test',self.uid,'catalog-test');self.rid=self.rs['id'];rdb.set_reseller_status(self.rid,'active')
        self.key,self.secret=rdb.create_api_key(self.rid,'test');self.headers=fixtures._hdr(self.key,self.secret);self.api=fixtures.Phase16AdminFailOrderTests.client
    def product(self,name='Master <Product> & ☃',price='10.01',stock=3,imported=True,custom=None,active=1,access=True,enabled=True,codes=0):
        db.cur.execute("INSERT INTO products(name,price,stock,delivery,active) VALUES(?,?,?,'manual',?)",(name,float(price),stock,active));pid=db.cur.lastrowid;db.conn.commit()
        pub=rdb.ensure_product_public_id(pid);rdb.set_product_access(self.rid,pid,access)
        if imported:
            rdb.import_reseller_product(self.rid,pid,custom_name=custom)
            if not enabled:rdb.update_reseller_product_mapping(self.rid,pid,enabled=False)
        for i in range(codes):db.cur.execute('INSERT INTO product_stock(product_id,delivery,sold) VALUES(?,?,0)',(pid,'SYNTHETIC-NOT-DISPLAYED-'+str(i)))
        db.conn.commit();return pid,pub
    def scalar(self,sql,*args):db.cur.execute(sql,args);return db.cur.fetchone()[0]
    def endpoint(self,url):
        result=self.api.get(url,headers=self.headers);self.assertEqual(result.status_code,200,result.get_json());return result.get_json()['data']
    def callback(self,data):
        q=SimpleNamespace(answer=AsyncMock())
        with patch.object(h,'edit_or_send',new=AsyncMock()) as edit:
            self.assertTrue(asyncio.run(h.handle_reseller_callback(q,self.uid,data,SimpleNamespace())))
            return edit.await_args.args[1],edit.await_args.args[2],q
    def page_ids(self,markup):
        return [b.callback_data.split(':')[2] for row in markup.inline_keyboard for b in row if b.callback_data.startswith('res_product:detail:')]
    def visible_api(self):
        return [p for p in self.endpoint('/api/v1/my-products')['products'] if p['enabled'] and p['master_active'] and p['access_enabled']]
    def context(self):
        principal=authenticate_credentials(self.key,self.secret,'test')
        return ApiRequestContext('a'*32,principal.environment,principal.reseller_id,principal.reseller_public_id,principal.api_key_id,principal.api_key_public_id)
    def test_api_parity_names_prices_enabled_and_availability(self):
        a,pa=self.product(custom='Custom <name> & 🔑',codes=2,stock=9);b,pb=self.product(name='Plain product',stock=0);c,pc=self.product();d,pd=self.product()
        rdb.set_pricing_rule(self.rid,a,'fixed_price',value=777)
        rdb.set_pricing_rule(self.rid,b,'markup_percent',value=1234)
        rdb.set_pricing_rule(self.rid,c,'discount_percent',value=500)
        api=self.visible_api();prices={p['product_id']:p for p in self.endpoint('/api/v1/pricing')['prices']}
        products=h.read_reseller_telegram_catalog(self.uid);self.assertEqual({p['product_id'] for p in products},{p['product_id'] for p in api})
        for item in products:
            reference=next(p for p in api if p['product_id']==item['product_id'])
            self.assertEqual(item['name'],reference['custom_name'] or reference['master_name'])
            for k in ('price','currency','availability','enabled'):self.assertEqual(item[k],reference[k])
            self.assertEqual(item['price'],prices[item['product_id']]['price'])
        self.assertEqual(h.read_reseller_telegram_catalog(self.uid,'pricing'),[{k:p[k] for k in ('product_id','name','price','currency')} for p in self.endpoint('/api/v1/pricing')['prices']])
        self.assertEqual(next(p for p in products if p['product_id']==pb)['availability'],'out_of_stock')
    def test_custom_name_fallback_and_exact_money(self):
        pid,pub=self.product(custom='Reseller name');rdb.set_pricing_rule(self.rid,pid,'discount_percent',value=3333)
        before=h.read_reseller_telegram_catalog(self.uid)[0];self.assertEqual(before['name'],'Reseller name');self.assertEqual(before['price'],'6.67')
        rdb.update_reseller_product_mapping(self.rid,pid,custom_name=None);after=h.read_reseller_telegram_catalog(self.uid)[0];self.assertEqual(after['name'],'Master <Product> & ☃')
    def test_only_current_imported_accessible_enabled_products(self):
        _,valid=self.product();self.product(active=0);self.product(access=False);self.product(enabled=False);self.product(imported=False)
        self.assertEqual([p['product_id'] for p in h.read_reseller_telegram_catalog(self.uid)],[valid])
        self.assertEqual({p['product_id'] for p in h.read_reseller_telegram_catalog(self.uid)},{p['product_id'] for p in self.visible_api()})
    def test_pricing_api_broader_scope_does_not_change_api_contract(self):
        self.product(imported=False);self.product(enabled=False);self.product(active=0);self.product(access=False)
        api=self.endpoint('/api/v1/pricing')['prices'];telegram=h.read_reseller_telegram_catalog(self.uid,'pricing')
        self.assertEqual(len(api),2);self.assertEqual({p['product_id'] for p in api},{p['product_id'] for p in telegram});self.assertEqual(h.read_reseller_telegram_catalog(self.uid),[])
        text,_=h.build_reseller_pricing(telegram);self.assertIn('Prices are managed by the owner and reflect your current effective reseller prices.',text)
    def test_large_catalog_117_mappings_no_truncation_or_duplicates(self):
        for n in range(117):self.product(name=f'Catalog {n}',stock=n%3)
        products=h.read_reseller_telegram_catalog(self.uid);api=self.visible_api();self.assertEqual(len(products),117)
        visited=[]
        for page in range(1,16):
            text,markup=h.build_reseller_products(products,page);ids=self.page_ids(markup);self.assertLessEqual(len(ids),8);visited+=ids
        self.assertEqual(len(visited),117);self.assertEqual(len(set(visited)),117);self.assertEqual(set(visited),{p['product_id'] for p in api})
        prices=h.read_reseller_telegram_catalog(self.uid,'pricing');price_ids=[]
        for page in range(1,16):
            text,_=h.build_reseller_pricing(prices,page);ids=re.findall(r'<code>(PRD-[A-HJ-NP-Z2-9]{12})</code>',text);self.assertLessEqual(len(ids),8);price_ids+=ids
        self.assertEqual(len(price_ids),117);self.assertEqual(len(set(price_ids)),117);self.assertEqual(set(price_ids),{p['product_id'] for p in self.endpoint('/api/v1/pricing')['prices']})
    def test_empty_state_docs_and_retired_placeholder(self):
        for kind in ('products','pricing'):
            text,markup,_=self.callback(f'res_{kind}:page:1');self.assertIn('No products imported yet.',text);self.assertIn('See 📚 API Docs',text)
            self.assertIn('res_api_docs',[b.callback_data for row in markup.inline_keyboard for b in row])
        q=SimpleNamespace(answer=AsyncMock())
        self.assertFalse(asyncio.run(h.handle_reseller_callback(q,self.uid,'res_soon',None)));self.assertEqual(q.answer.await_count,0)
    def test_master_disable_between_list_and_detail_no_stale_card(self):
        pid,pub=self.product(custom='DO-NOT-SHOW-AFTER-DISABLE');self.callback('res_products:page:1')
        db.cur.execute('UPDATE products SET active=0 WHERE id=?',(pid,));db.conn.commit()
        text,markup,_=self.callback(f'res_product:detail:{pub}:1');self.assertIn('no longer accessible',text);self.assertNotIn('DO-NOT-SHOW-AFTER-DISABLE',text);self.assertEqual(self.page_ids(markup),[])
        self.assertEqual(h.read_reseller_telegram_catalog(self.uid,'pricing'),[])
    def test_mapping_disable_access_revoke_remove_all_refresh(self):
        pid,pub=self.product()
        for mode in ('mapping','access','remove'):
            if mode=='mapping':rdb.update_reseller_product_mapping(self.rid,pid,enabled=False)
            elif mode=='access':rdb.update_reseller_product_mapping(self.rid,pid,enabled=True);rdb.set_product_access(self.rid,pid,False)
            else:rdb.set_product_access(self.rid,pid,True);rdb.remove_reseller_product_mapping(self.rid,pid)
            text,markup,_=self.callback(f'res_product:detail:{pub}:1');self.assertIn('no longer accessible',text);self.assertEqual(self.page_ids(markup),[])
    def test_hard_delete_and_invalid_page_clamps_after_catalog_changes(self):
        records=[self.product() for _ in range(9)];self.assertEqual(len(self.page_ids(self.callback('res_products:page:2')[1])),1)
        db.cur.execute('DELETE FROM products WHERE id=?',(records[0][0],));db.conn.commit()
        text,markup,_=self.callback('res_products:page:2');self.assertIn('Page 1/1',text);self.assertEqual(len(self.page_ids(markup)),8)
    def test_inactive_status_block_and_no_reseller_denied(self):
        self.product()
        for status in ('pending','suspended','closed'):
            rdb.set_reseller_status(self.rid,status)
            for action in ('res_products:page:1','res_pricing:page:1'):
                text,_,_=self.callback(action);self.assertIn('Active reseller access required',text)
            with self.assertRaises(PermissionError):h.read_reseller_telegram_catalog(self.uid)
        rdb.set_reseller_status(self.rid,'active');db.cur.execute('UPDATE users SET blocked=1 WHERE user_id=?',(self.uid,));db.conn.commit()
        with self.assertRaises(PermissionError):h.read_reseller_telegram_catalog(self.uid,'pricing')
        with self.assertRaises(PermissionError):h.read_reseller_telegram_catalog(self.uid+1000000)
    def test_authorization_rechecked_for_pagination_and_detail(self):
        _,pub=self.product();self.callback('res_products:page:1');rdb.set_reseller_status(self.rid,'suspended')
        for action in ('res_products:page:2','res_pricing:page:2',f'res_product:detail:{pub}:1'):
            text,_,_=self.callback(action);self.assertIn('Active reseller access',text);self.assertNotIn(pub,text)
    def test_cross_reseller_idor_and_invalid_detail_identifiers(self):
        _,pub=self.product();other_uid=self.uid+2000000;db.ensure_user_id(other_uid);other=rdb.create_reseller('Other',other_uid,'other');rdb.set_reseller_status(other['id'],'active')
        with patch.object(h,'edit_or_send',new=AsyncMock()) as edit:
            asyncio.run(h.handle_reseller_callback(SimpleNamespace(),other_uid,f'res_product:detail:{pub}:1',None));text=edit.await_args.args[1]
            self.assertIn('no longer accessible',text);self.assertNotIn(pub,text)
        for value in ('123','PRD-INVALID','PRD-AAAAAAAAAAAA;DROP TABLE products'):
            text,_,_=self.callback(f'res_product:detail:{value}:1');self.assertIn('Invalid catalog link',text)
    def test_malformed_navigation_safe_and_valid_out_of_range_clamps(self):
        self.product()
        for kind in ('products','pricing'):
            for val in ('abc','1.0','+2','', '9'*500):self.assertIn('Invalid catalog link',self.callback(f'res_{kind}:page:{val}')[0])
            for val in ('-2','0','9999999999'):
                text,_,_=self.callback(f'res_{kind}:page:{val}');self.assertIn('Page 1/1',text)
    def test_html_escape_and_no_price_provenance_or_sensitive_fields(self):
        pid,pub=self.product(name='<script>MASTER&NAME</script>',custom='<b>CUSTOM & NAME</b>');rdb.set_pricing_rule(self.rid,pid,'fixed_price',value=135)
        api=self.endpoint('/api/v1/pricing')['prices'];self.assertIn('price_source',api[0])
        items=h.read_reseller_telegram_catalog(self.uid);prices=h.read_reseller_telegram_catalog(self.uid,'pricing')
        for product in items+prices:
            self.assertNotIn('price_source',product);self.assertNotIn('rule_type',product)
        texts=[h.build_reseller_products(items)[0],h.build_reseller_product_detail(items[0])[0],h.build_reseller_pricing(prices)[0]]
        for text in texts:
            self.assertNotIn('<script>',text);self.assertNotIn('<b>CUSTOM & NAME</b>',text)
            for forbidden in ('price_source','fixed_price','master_price','value_bps','master_tier','SYNTHETIC-NOT-DISPLAYED'):self.assertNotIn(forbidden,text)
        self.assertIn('&lt;b&gt;CUSTOM &amp; NAME&lt;/b&gt;',texts[0])
    def test_prices_availability_and_names_recomputed_every_interaction(self):
        pid,pub=self.product(stock=0,custom='Before')
        text,_,_=self.callback(f'res_product:detail:{pub}:1');self.assertIn('10.01 USDT',text);self.assertIn('Out of stock',text)
        rdb.update_reseller_product_mapping(self.rid,pid,custom_name='After');rdb.set_pricing_rule(self.rid,pid,'fixed_price',value=899)
        db.cur.execute('UPDATE products SET stock=4 WHERE id=?',(pid,));db.conn.commit()
        text,_,_=self.callback(f'res_product:detail:{pub}:1');self.assertIn('8.99 USDT',text);self.assertIn('After',text);self.assertIn('In stock',text)
        price_text,_,_=self.callback('res_pricing:page:1');self.assertIn('8.99 USDT',price_text)
    def test_pure_builders_deterministic_no_db_or_global_auth(self):
        self.product();items=h.read_reseller_telegram_catalog(self.uid);prices=h.read_reseller_telegram_catalog(self.uid,'pricing')
        with patch.object(rdb,'get_cursor',side_effect=AssertionError('hidden DB read')),patch.object(h,'get_active_reseller',side_effect=AssertionError('hidden auth read')):
            self.assertEqual(h.build_reseller_products(items),h.build_reseller_products(items))
            self.assertEqual(h.build_reseller_pricing(prices),h.build_reseller_pricing(prices));h.build_reseller_product_detail(items[0])
    def test_legacy_long_names_message_budget_without_changing_semantic_names(self):
        name=('<&>'*2000)+'🧩'
        for n in range(8):self.product(name=name)
        items=h.read_reseller_telegram_catalog(self.uid);prices=h.read_reseller_telegram_catalog(self.uid,'pricing')
        self.assertEqual(items[0]['name'],name)
        for text in (h.build_reseller_products(items)[0],h.build_reseller_pricing(prices)[0],h.build_reseller_product_detail(items[0])[0]):
            self.assertLess(len(text),4096);self.assertIn('…',text);self.assertNotIn('<&>',text)
    def test_readonly_entrypoint_does_not_write_or_create_api_keys(self):
        self.product();before=db.get_conn().total_changes;key_count=self.scalar('SELECT COUNT(*) FROM reseller_api_keys WHERE reseller_id=?',self.rid)
        for kind in ('products','pricing'):self.callback(f'res_{kind}:page:1')
        self.assertEqual(db.get_conn().total_changes,before);self.assertEqual(self.scalar('SELECT COUNT(*) FROM reseller_api_keys WHERE reseller_id=?',self.rid),key_count)
    def test_readonly_missing_identifier_fails_closed_without_backfill(self):
        pid,pub=self.product(imported=False);db.cur.execute('UPDATE products SET public_id=NULL WHERE id=?',(pid,));db.conn.commit();before=db.get_conn().total_changes
        with self.assertRaises(PricingError):h.read_reseller_telegram_catalog(self.uid,'pricing')
        # An unrelated unimported identifier cannot block the Products subset.
        self.assertEqual(h.read_reseller_telegram_catalog(self.uid),[])
        self.assertIsNone(self.scalar('SELECT public_id FROM products WHERE id=?',pid));self.assertEqual(db.get_conn().total_changes,before)
        # Existing API retains its historical lazy allocation, not changed.
        self.assertEqual(len(self.endpoint('/api/v1/pricing')['prices']),1)
    def test_legacy_imported_missing_prd_is_not_fabricated_or_exposed(self):
        pid,pub=self.product();db.cur.execute('UPDATE products SET public_id=NULL WHERE id=?',(pid,));db.conn.commit();before=db.get_conn().total_changes
        with self.assertRaises(ValueError):h.read_reseller_telegram_catalog(self.uid)
        text,_,_=self.callback('res_products:page:1');self.assertIn('owner/API initialization',text)
        self.assertIsNone(self.scalar('SELECT public_id FROM products WHERE id=?',pid));self.assertEqual(db.get_conn().total_changes,before)
    def test_engine_failure_does_not_disclose_internal_price_data(self):
        pid,pub=self.product();secret='SYNTHETIC-PRIVATE-MASTER-PRICE'
        db.cur.execute('UPDATE products SET price=? WHERE id=?',(secret,pid));db.conn.commit()
        with self.assertLogs('oggystore_bot',level='ERROR') as captured:text,_,_=self.callback('res_pricing:page:1')
        self.assertIn('temporarily unavailable',text);self.assertNotIn(secret,text+str(captured.output))
    def test_no_api_key_required_for_telegram_and_api_authorization_unchanged(self):
        self.product();rdb.revoke_api_key_for_reseller(self.rid,self.key)
        self.assertEqual(len(h.read_reseller_telegram_catalog(self.uid)),1)
        self.assertNotEqual(self.api.get('/api/v1/pricing',headers=self.headers).status_code,200)
        with self.assertRaises(product_import.ProductImportError):product_import.list_mappings(None,'test')
        with self.assertRaises(PricingError):pricing.list_product_prices(None,'test')
    def test_center_routes_all_stubs_retired_and_wallet_docs_preserved(self):
        text,markup=h.build_reseller_home(self.uid);buttons=[b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn('res_products:page:1',buttons);self.assertIn('res_pricing:page:1',buttons);self.assertEqual(buttons.count('res_soon'),0);self.assertIn('res_stats',buttons)
        for cb in ('res_wallet','res_keys','res_api_docs','res_webhooks'):self.assertIn(cb,buttons)
        self.assertIn('Reseller Wallet',self.callback('res_wallet')[0]);self.assertTrue(self.callback('res_api_docs')[0]);self.assertFalse(asyncio.run(h.handle_reseller_callback(SimpleNamespace(),'invalid','res_unknown',None)))
if __name__=='__main__':unittest.main()
