"""Prompt 16.5: real API reads, owner delivery, bounded opt-in normalization."""
import csv
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import tests.test_phase16_manual_delivery as fixtures
from reseller_api.services.order_status import (api_order_status, derive_order_status,
    status_filter_sql, OrderStatusUnavailable)
from reseller_api.services.delivery_normalization import (normalize_empty_deliveries,
    normalization_ready, STATE_KEY)
from reseller_api.services.reconciliation import reconcile_reseller
from utils.crypto import encrypt_text, decrypt_text

db,rdb=fixtures.db,fixtures.rdb
ROOT=Path(__file__).resolve().parents[1]

class Phase16StatusTruthfulnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.Phase16ManualDeliveryTests.setUpClass()
    @classmethod
    def tearDownClass(cls):
        fixtures.Phase16ManualDeliveryTests.tearDownClass()
    def setUp(self):
        self.fx=fixtures.Phase16ManualDeliveryTests('test_pending_to_delivered_real_endpoint_ciphertext_and_event')
        self.fx.setUp(); self.addCleanup(self.fx.doCleanups)
        self.api=self.fx.api
        db.cur.execute('DELETE FROM settings WHERE key=?',(STATE_KEY,));db.conn.commit()
    def pending(self):
        return self.fx.pending()
    def stored(self,oid):
        db.cur.execute('SELECT delivery,master_status,updated FROM orders WHERE id=?',(oid,))
        return tuple(db.cur.fetchone())
    def set_delivery(self,oid,value,label=None):
        db.cur.execute('UPDATE orders SET delivery=?,master_status=? WHERE id=?',(value,label,oid));db.conn.commit()
    def detail(self,oid,h):
        r=self.api.get(f'/api/v1/orders/ORD-{oid:08d}',headers=h)
        self.assertEqual(r.status_code,200,r.get_json());return r.get_json()['data']['order']
    def listed(self,h,status=None):
        return self.api.get('/api/v1/orders'+('?'+'status='+status if status else ''),headers=h)
    def normalized(self):
        result=normalize_empty_deliveries(apply=True)
        self.assertTrue(result['ready'],result)
        return result
    def export(self,h):
        r=self.api.get('/api/v1/exports/orders.csv',headers=h)
        self.assertEqual(r.status_code,200)
        return list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    def test_creation_null_and_processing_all_read_paths(self):
        rs,oid,pid,h=self.pending()
        self.assertIsNone(self.stored(oid)[0])
        self.assertEqual(api_order_status(oid),'processing')
        self.assertEqual(derive_order_status(oid).delivery_status,'pending')
        d=self.detail(oid,h)
        self.assertEqual((d['status'],d['delivery_status']),('processing','pending'))
        self.assertEqual(self.listed(h).get_json()['data']['orders'][0]['status'],'processing')
        self.assertEqual(self.export(h)[0]['status'],'processing')
        # Replay goes through the actual create controller, preserving stored data.
        db.cur.execute('SELECT client_order_id FROM orders WHERE id=?',(oid,));coid=db.cur.fetchone()[0]
        pub=rdb.ensure_product_public_id(pid)
        replay=self.api.post('/api/v1/orders',headers=h,json={'product_id':pub,'quantity':1,'client_order_id':coid})
        self.assertEqual(replay.status_code,200,replay.get_json())
        self.assertEqual((replay.get_json()['data']['status'],replay.get_json()['data']['delivery_status']),('processing','pending'))
        self.assertEqual(reconcile_reseller(rs['id']),[])
    def test_quote_contract_has_no_status(self):
        rs,oid,pid,h=self.pending()
        q=self.api.post('/api/v1/orders/quote',headers=h,json={'product_id':rdb.ensure_product_public_id(pid),'quantity':1})
        self.assertEqual(q.status_code,200,q.get_json())
        self.assertNotIn('status',q.get_json()['data'])
        self.assertNotIn('delivery_status',q.get_json()['data'])
    def test_actual_owner_delivery_processing_to_completed(self):
        rs,oid,pid,h=self.pending();self.normalized()
        self.assertEqual(self.listed(h,'processing').get_json()['data']['orders'][0]['order_id'],f'ORD-{oid:08d}')
        response=self.fx.deliver(oid,'Unicode 🧩 <code>real</code>')
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(self.detail(oid,h)['status'],'completed')
        self.assertEqual(self.export(h)[0]['status'],'completed')
        self.assertEqual(self.listed(h,'processing').get_json()['data']['orders'],[])
        self.assertEqual(self.listed(h,'completed').get_json()['data']['orders'][0]['status'],'completed')
        self.assertEqual(self.fx.read(oid,h)['items'],['Unicode 🧩 <code>real</code>'])
        self.assertEqual(reconcile_reseller(rs['id']),[])
    def test_legacy_cipher_empty_status_and_guarded_filter(self):
        rs,oid,pid,h=self.pending();token=encrypt_text('');self.set_delivery(oid,token,'✅ Completed')
        self.assertEqual(self.detail(oid,h)['status'],'processing')
        self.assertEqual(self.export(h)[0]['status'],'processing')
        for status in ('processing','completed'):
            response=self.listed(h,status)
            self.assertEqual(response.status_code,409,response.get_json())
            self.assertEqual(response.get_json()['error']['code'],'delivery_normalization_required')
        self.assertEqual(self.stored(oid)[0],token)
        self.normalized()
        self.assertIsNone(self.stored(oid)[0])
        self.assertEqual(self.listed(h,'processing').get_json()['data']['orders'][0]['status'],'processing')
    def test_legacy_null_master_with_content_completed(self):
        rs,oid,pid,h=self.pending();token=encrypt_text('x');self.set_delivery(oid,token,None)
        self.assertEqual(self.detail(oid,h)['status'],'completed')
        self.normalized();self.assertEqual(self.stored(oid)[0],token)
        self.assertEqual(self.listed(h,'completed').get_json()['data']['orders'][0]['status'],'completed')
    def test_raw_legacy_empty_and_null_are_processing(self):
        rs,oid,pid,h=self.pending()
        for value in (None,''):
            self.set_delivery(oid,value,None)
            self.assertEqual(api_order_status(oid),'processing')
            self.normalized();self.assertEqual(self.listed(h,'processing').status_code,200)
    def test_real_short_content_same_cipher_length_preserved(self):
        rs,oid,pid,h=self.pending();empty=encrypt_text('');actual=encrypt_text('x')
        self.assertEqual(len(empty),len(actual))
        self.set_delivery(oid,actual);self.normalized()
        self.assertEqual(self.stored(oid)[0],actual);self.assertEqual(api_order_status(oid),'completed')
    def test_whitespace_is_actual_content_preserved(self):
        rs,oid,pid,h=self.pending();token=encrypt_text(' \n\t')
        self.set_delivery(oid,token);self.normalized()
        self.assertEqual(self.stored(oid)[0],token);self.assertEqual(api_order_status(oid),'completed')
    def test_terminal_precedence_and_filter(self):
        rs,oid,pid,h=self.pending()
        for label,expected in [('⚠ Failed','failed'),('❌ Cancelled','cancelled'),('Canceled','cancelled')]:
            self.set_delivery(oid,None,label)
            self.assertEqual(api_order_status(oid),expected)
            self.assertEqual(self.listed(h,expected).get_json()['data']['orders'][0]['status'],expected)
            self.normalized();self.assertEqual(self.listed(h,'processing').get_json()['data']['orders'],[])
    def test_replacement_precedence_and_sql_parity(self):
        rs,oid,pid,h=self.pending()
        for legacy,expected in [('pending','warranty_review'),('approved','replaced'),('auto','replaced'),('rejected','processing')]:
            db.cur.execute('INSERT INTO replacements(order_id,user_id,status) VALUES(?,0,?)',(oid,legacy));db.conn.commit()
            self.assertEqual(api_order_status(oid),expected)
            self.normalized()
            matches=self.listed(h,'processing').get_json()['data']['orders']
            self.assertEqual(bool(matches),expected=='processing')
            self.set_delivery(oid,None,'⚠ Failed');self.assertEqual(api_order_status(oid),'failed')
            self.set_delivery(oid,None,None)
    def test_unreadable_not_guessed_and_cleanup_blocks_readiness(self):
        rs,oid,pid,h=self.pending();token='enc:malformed';self.set_delivery(oid,token)
        with self.assertRaises(OrderStatusUnavailable):api_order_status(oid)
        response=self.api.get(f'/api/v1/orders/ORD-{oid:08d}',headers=h)
        self.assertEqual(response.status_code,503,response.get_json())
        result=normalize_empty_deliveries(apply=True)
        self.assertGreater(result['unreadable'],0);self.assertFalse(result['ready'])
        self.assertEqual(self.stored(oid)[0],token)
        # Repair only the synthetic fixture for following tests.
        self.set_delivery(oid,None)
    def test_missing_key_never_classifies_ciphertext(self):
        rs,oid,pid,h=self.pending();token=encrypt_text('');self.set_delivery(oid,token)
        with patch.dict(os.environ,{'DATA_ENCRYPTION_KEY':''}):
            with self.assertRaises(OrderStatusUnavailable):api_order_status(oid)
            result=normalize_empty_deliveries(apply=True)
            self.assertFalse(result['ready']);self.assertGreater(result['unreadable'],0)
        self.assertEqual(self.stored(oid)[0],token)
    def test_dry_run_no_row_settings_schema_or_financial_writes(self):
        rs,oid,pid,h=self.pending();self.set_delivery(oid,encrypt_text(''),'🔄 Processing')
        before='\n'.join(db.conn.iterdump())
        result=normalize_empty_deliveries()
        self.assertTrue(result['dry_run']);self.assertGreater(result['empty'],0);self.assertEqual(result['changed'],0)
        self.assertEqual('\n'.join(db.conn.iterdump()),before)
        self.assertFalse(normalization_ready())
    def test_apply_only_authenticated_empty_reseller_rows(self):
        rs,oid,pid,h=self.pending();token=encrypt_text('');self.set_delivery(oid,token,'❌ Cancelled')
        rs2,real,pid2,h2=self.pending();actual=encrypt_text('DELIVERY-REAL');self.set_delivery(real,actual)
        db.cur.execute("INSERT INTO orders(user_id,product_name,price,delivery,source) VALUES(1,'Customer',1,?,'customer')",(token,))
        customer=db.cur.lastrowid;db.conn.commit()
        before_label=self.stored(oid)[1:];self.normalized()
        self.assertEqual(self.stored(oid),(None,*before_label))
        self.assertEqual(self.stored(real)[0],actual)
        self.assertEqual(self.stored(customer)[0],token)
    def test_money_ledger_analytics_totals_unchanged_by_cleanup(self):
        rs,oid,pid,h=self.pending();self.set_delivery(oid,encrypt_text(''))
        before=rdb.get_reseller_sales_analytics(rs['id'],days=30)
        db.cur.execute('SELECT * FROM reseller_wallet_transactions WHERE reseller_id=?',(rs['id'],));ledger=db.cur.fetchall()
        balance=rdb.get_wallet_balance_cents(rs['id'])
        self.normalized();after=rdb.get_reseller_sales_analytics(rs['id'],days=30)
        for field in ('gross_sales_cents','net_sales_cents','refunds_total_cents','quantity_sold','orders_total'):
            self.assertEqual(after[field],before[field],field)
        self.assertEqual(before['completed'],1);self.assertEqual(after['completed'],0)
        self.assertEqual(after['pending'],1);self.assertEqual(before['quantity_sold'],1)
        self.assertEqual(rdb.get_wallet_balance_cents(rs['id']),balance)
        db.cur.execute('SELECT * FROM reseller_wallet_transactions WHERE reseller_id=?',(rs['id'],));self.assertEqual(db.cur.fetchall(),ledger)
        self.assertEqual(reconcile_reseller(rs['id']),[])
    def test_bounded_apply_resume_and_idempotency(self):
        rs,oid,pid,h=self.pending();self.set_delivery(oid,encrypt_text(''))
        result=normalize_empty_deliveries(apply=True,batch_size=1,max_rows=1)
        self.assertEqual(result['rows_this_invocation'],1)
        while result['needs_resume']:
            result=normalize_empty_deliveries(apply=True,batch_size=2,max_rows=2,resume=True)
            self.assertLessEqual(result['rows_this_invocation'],2)
        self.assertTrue(result['ready']);self.assertIsNone(self.stored(oid)[0])
        self.assertEqual(self.normalized()['changed'],0)
    def test_bounds_rejected(self):
        for kwargs in ({'batch_size':0},{'batch_size':501},{'max_rows':0},{'max_rows':50001}):
            with self.assertRaises(ValueError):normalize_empty_deliveries(**kwargs)
    def test_index_bounded_large_table_explain(self):
        rs,oid,pid,h=self.pending()
        db.cur.executemany("INSERT INTO orders(user_id,product_name,price,source,reseller_id,delivery) VALUES(1,'Synthetic',0,'reseller_api',?,NULL)",[(rs['id'],)]*1500);db.conn.commit()
        self.normalized()
        sql="SELECT id FROM orders WHERE reseller_id=? AND source='reseller_api' AND id<? AND "+status_filter_sql('processing')+' ORDER BY id DESC LIMIT ?'
        db.cur.execute('EXPLAIN QUERY PLAN '+sql,(rs['id'],oid+2000,21))
        plan=' '.join(str(row[3]) for row in db.cur.fetchall())
        self.assertIn('SEARCH orders',plan);self.assertIn('idx_orders_reseller',plan)
        self.assertNotIn('SCAN orders',plan)
        response=self.listed(h,'processing');self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(len(response.get_json()['data']['orders']),20)
    def test_mapper_imports_are_same_function_no_duplicate_definitions(self):
        import reseller_api.controllers.v1.orders as controller
        import reseller_api.services.order_query as query
        self.assertIs(controller.api_order_status,api_order_status);self.assertIs(query.api_order_status,api_order_status)
        for module in (controller,query):
            self.assertNotIn('def _api_status(',Path(module.__file__).read_text())
    def test_startup_does_not_encrypt_raw_empty_reseller_delivery(self):
        rs,oid,pid,h=self.pending();self.set_delivery(oid,'')
        db.cur.execute("INSERT INTO orders(user_id,product_name,price,delivery,source) VALUES(1,'Customer',1,'','customer')")
        customer=db.cur.lastrowid;db.conn.commit()
        db.encrypt_existing_secrets()
        self.assertEqual(self.stored(oid)[0],'')
        self.assertTrue(self.stored(customer)[0].startswith('enc:'))
    def test_cli_dry_run_enforced_read_only_no_import_migrations(self):
        rs,oid,pid,h=self.pending();self.set_delivery(oid,encrypt_text(''))
        before='\n'.join(db.conn.iterdump())
        env=os.environ.copy();env['DATABASE_URL']=''
        response=subprocess.run([sys.executable,'tools/normalize_reseller_empty_delivery.py','--db-path',str(db.DB_PATH)],cwd=ROOT,env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(response.returncode,0,response.stderr)
        result=json.loads(response.stdout.splitlines()[-1]);self.assertTrue(result['dry_run']);self.assertEqual(result['changed'],0)
        self.assertEqual('\n'.join(db.conn.iterdump()),before)
    def test_cli_explicit_apply_normalizes_only_empty(self):
        rs,oid,pid,h=self.pending();token=encrypt_text('');self.set_delivery(oid,token)
        env=os.environ.copy();env['DATABASE_URL']=''
        response=subprocess.run([sys.executable,'tools/normalize_reseller_empty_delivery.py',
            '--db-path',str(db.DB_PATH),'--apply','--confirm','NORMALIZE_EMPTY_RESELLER_DELIVERY'],
            cwd=ROOT,env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(response.returncode,0,response.stderr)
        result=json.loads(response.stdout.splitlines()[-1]);self.assertTrue(result['ready'])
        self.assertFalse(result['dry_run']);self.assertIsNone(self.stored(oid)[0])
        self.assertEqual(self.listed(h,'processing').status_code,200)

    def test_cli_apply_requires_confirmation(self):
        response=subprocess.run([sys.executable,'tools/normalize_reseller_empty_delivery.py','--apply'],cwd=ROOT,capture_output=True,text=True,timeout=20)
        self.assertEqual(response.returncode,2);self.assertIn('--confirm',response.stderr)
    def test_owner_ui_processing_customer_display_unchanged(self):
        rs,oid,pid,h=self.pending()
        response=self.fx.owner.get('/api/orders')
        self.assertEqual(response.status_code,200,response.get_json())
        rows=response.get_json()['orders'];row=next(r for r in rows if r['id']==oid)
        self.assertEqual(row['status_label'],'🔄 Processing')
    def test_create_response_directly_reports_processing(self):
        rs,oid,pid,h=self.pending()
        response=self.api.post('/api/v1/orders',headers=h,
            json={'product_id':rdb.ensure_product_public_id(pid),'quantity':1,
                  'client_order_id':f'status-new-{oid}'})
        self.assertEqual(response.status_code,201,response.get_json())
        self.assertEqual((response.get_json()['data']['status'],response.get_json()['data']['delivery_status']),('processing','pending'))
    def test_future_static_encrypted_empty_stored_as_null(self):
        rs,oid,pid,h=self.pending()
        db.cur.execute('UPDATE products SET delivery=? WHERE id=?',(encrypt_text(''),pid));db.conn.commit()
        response=self.api.post('/api/v1/orders',headers=h,
            json={'product_id':rdb.ensure_product_public_id(pid),'quantity':1,
                  'client_order_id':f'static-empty-{oid}'})
        self.assertEqual(response.status_code,201,response.get_json())
        new_id=int(response.get_json()['data']['order_id'].replace('ORD-',''))
        self.assertIsNone(self.stored(new_id)[0]);self.assertEqual(api_order_status(new_id),'processing')
    def test_safe_delivery_writer_normalizes_future_empty(self):
        from reseller_api.services.order_idempotency import safe_write_delivery
        rs,oid,pid,h=self.pending()
        self.assertEqual(safe_write_delivery(oid,encrypt_text('')),'written')
        self.assertIsNone(self.stored(oid)[0]);self.assertEqual(api_order_status(oid),'processing')
        self.assertEqual(safe_write_delivery(oid,encrypt_text('real')),'written')
        self.assertEqual(api_order_status(oid),'completed')
    def test_export_unreadable_fails_before_csv_headers(self):
        rs,oid,pid,h=self.pending();self.set_delivery(oid,'enc:malformed')
        response=self.api.get('/api/v1/exports/orders.csv',headers=h)
        self.assertEqual(response.status_code,503,response.get_json())
        self.assertEqual(response.get_json()['error']['code'],'order_state_unavailable')
        self.set_delivery(oid,None)
    def test_apply_schema_statements_and_terminal_labels_preserved(self):
        from reseller_api.services.statement_service import generate_statement
        from utils.timezone import now_text
        rs,oid,pid,h=self.pending();self.set_delivery(oid,encrypt_text(''),'❌ Cancelled')
        day=now_text()[:10]
        before=generate_statement(rs['id'],period_type='custom',start_date=day,end_date=day)
        db.cur.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','index','trigger') ORDER BY type,name")
        schema=db.cur.fetchall();self.normalized()
        db.cur.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','index','trigger') ORDER BY type,name")
        self.assertEqual(db.cur.fetchall(),schema)
        self.assertEqual(api_order_status(oid),'cancelled')
        after=generate_statement(rs['id'],period_type='custom',start_date=day,end_date=day)
        for key in ('purchases_cents','refunds_cents','closing_balance_cents','opening_balance_cents'):
            self.assertEqual(after[key],before[key],key)
    def test_customer_owner_display_legacy_behavior_preserved(self):
        rs,oid,pid,h=self.pending()
        db.cur.execute("INSERT INTO orders(user_id,product_name,price,delivery,source,master_status) VALUES(1,'Customer',1,NULL,'customer','Processing')")
        customer=db.cur.lastrowid;db.conn.commit()
        response=self.fx.owner.get('/api/orders')
        row=next(r for r in response.get_json()['orders'] if r['id']==customer)
        self.assertEqual(row['status_label'],'✅ Completed')  # existing customer convention
        self.assertFalse(row['can_deliver']);self.assertFalse(row['can_cancel'])

    def test_all_sql_status_cases_match_resolver(self):
        rs,oid,pid,h=self.pending()
        for label in (None,'✅ Completed','🔄 Processing','⚠ Failed','❌ Cancelled','Canceled','Failed Cancelled'):
            for value in (None,'',encrypt_text('real')):
                self.set_delivery(oid,value,label);expected=api_order_status(oid)
                for status in ('processing','completed','failed','cancelled'):
                    db.cur.execute('SELECT id FROM orders WHERE id=? AND '+status_filter_sql(status),(oid,))
                    self.assertEqual(db.cur.fetchone() is not None,status==expected,(label,value is None,status,expected))

if __name__=='__main__':unittest.main()
