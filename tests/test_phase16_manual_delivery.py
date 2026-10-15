"""Prompt 16.4: real owner route, atomic persistence, real v1 delivery endpoint."""
import json
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.test_phase16_admin_cancel_order as fixtures
import reseller_api.services.order_idempotency as idem
import reseller_api.services.webhook_outbox as outbox
from reseller_api.services.reconciliation import reconcile_reseller
from utils.crypto import decrypt_text, encrypt_text
from database.pgcompat import atomic

ROOT = Path(__file__).resolve().parents[1]
db, rdb = fixtures.db, fixtures.rdb


class Phase16ManualDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.Phase16AdminCancelOrderTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.Phase16AdminCancelOrderTests.tearDownClass()

    def setUp(self):
        self.fx = fixtures.Phase16AdminCancelOrderTests('test_allowed_path_status_refund_stock_webhook_audit')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.owner = self.fx.owner
        self.api = self.fx.fx.client

    def pending(self):
        rs, oid, pid = self.fx.pending(manual=True)
        key, secret = rdb.create_api_key(rs['id'], 'test')
        return rs, oid, pid, fixtures.support._hdr(key, secret)

    def deliver(self, oid, content='MANUAL-CODE-123', client=None):
        return (client or self.owner).post(f'/api/orders/{oid}/deliver',
            json={'delivery':content},headers={'X-CSRF-Token':'test-csrf'})

    def read(self, oid, headers):
        response = self.api.get(f'/api/v1/orders/ORD-{oid:08d}/delivery',headers=headers)
        self.assertEqual(response.status_code,200,response.get_json())
        return response.get_json()['data']['delivery']

    def snapshot(self, oid):
        db.cur.execute('SELECT delivery,master_status,updated FROM orders WHERE id=?',(oid,))
        return tuple(db.cur.fetchone())

    def completed_events(self, oid):
        db.cur.execute("SELECT payload_json FROM webhook_outbox WHERE aggregate_id=? AND event_type='delivery.completed'",(f'ORD-{oid:08d}',))
        return [json.loads(row[0]) for row in db.cur.fetchall()]

    def test_pending_to_delivered_real_endpoint_ciphertext_and_event(self):
        rs,oid,pid,headers=self.pending()
        before=self.read(oid,headers)
        self.assertEqual((before['delivery_status'],before['items']),('pending',[]))
        text='SECRET-MANUAL-ONE\n\nSECRET-MANUAL-TWO'
        response=self.deliver(oid,text)
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.get_json()['order_id'],f'ORD-{oid:08d}')
        self.assertEqual(response.get_json()['delivery_status'],'delivered')
        stored,label,updated=self.snapshot(oid)
        self.assertTrue(stored.startswith('enc:'))
        self.assertNotIn('SECRET-MANUAL',stored)
        self.assertEqual(decrypt_text(stored),text)
        self.assertIn('Completed',label);self.assertTrue(updated)
        actual=self.read(oid,headers)
        self.assertEqual(actual['delivery_status'],'delivered')
        self.assertEqual(actual['items'],['SECRET-MANUAL-ONE','SECRET-MANUAL-TWO'])
        events=self.completed_events(oid)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['delivery_status'],'delivered')
        self.assertEqual(events[0]['quantity'],1)
        self.assertNotIn('SECRET-MANUAL',json.dumps(events))
        self.assertNotIn('SECRET-MANUAL',json.dumps(response.get_json()))
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_repeat_is_200_note_no_overwrite_no_event(self):
        rs,oid,pid,headers=self.pending()
        self.assertEqual(self.deliver(oid,'original').status_code,200)
        original=self.snapshot(oid)
        response=self.deliver(oid,'overwrite attempt')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['note'],'delivery already present')
        self.assertEqual(self.snapshot(oid),original)
        self.assertEqual(self.read(oid,headers)['items'],['original'])
        self.assertEqual(len(self.completed_events(oid)),1)

    def test_cancelled_rejected(self):
        rs,oid,pid,headers=self.pending()
        self.assertEqual(self.fx.cancel(oid).status_code,200)
        original=self.snapshot(oid)
        self.assertEqual(self.deliver(oid).status_code,409)
        self.assertEqual(self.snapshot(oid),original)
        self.assertEqual(self.completed_events(oid),[])

    def test_failed_rejected(self):
        rs,oid,pid,headers=self.pending()
        self.assertEqual(self.fx.fx._fail(self.owner,oid).status_code,200)
        original=self.snapshot(oid)
        self.assertEqual(self.deliver(oid).status_code,409)
        self.assertEqual(self.snapshot(oid),original)
        self.assertEqual(self.completed_events(oid),[])

    def test_html_unicode_and_leading_trailing_whitespace_round_trip(self):
        rs,oid,pid,headers=self.pending()
        text='  <b>हैलो 🌟</b> & 日本語 <script>literal text</script>  '
        response=self.deliver(oid,text)
        self.assertEqual(response.status_code,200)
        self.assertEqual(decrypt_text(self.snapshot(oid)[0]),text)
        self.assertEqual(self.read(oid,headers)['items'],[text.strip()])
        self.assertNotIn('literal text',json.dumps(self.completed_events(oid)))

    def test_user_enc_prefix_is_encrypted_not_trusted(self):
        rs,oid,pid,headers=self.pending()
        text='enc:this-is-user-text-not-a-fernet-token'
        self.assertEqual(self.deliver(oid,text).status_code,200)
        stored=self.snapshot(oid)[0]
        self.assertNotEqual(stored,text)
        self.assertEqual(decrypt_text(stored),text)
        self.assertEqual(self.read(oid,headers)['items'],[text])

    def test_invalid_payloads_are_rejected_before_claim_or_writes(self):
        rs,oid,pid,headers=self.pending()
        original=self.snapshot(oid)
        for body in ({},{'delivery':''},{'delivery':{}},{'delivery':None},
                     {'delivery':100},{'delivery':[]},[],None):
            with self.subTest(body=body):
                with patch('reseller_api.services.order_idempotency.claim_worker_job') as claim:
                    response=self.owner.post(f'/api/orders/{oid}/deliver',json=body,
                                             headers={'X-CSRF-Token':'test-csrf'})
                    self.assertEqual(response.status_code,400)
                    claim.assert_not_called()
                self.assertEqual(self.snapshot(oid),original)
        self.assertEqual(self.completed_events(oid),[])

    def test_oversized_payload_413_unchanged(self):
        rs,oid,pid,headers=self.pending()
        original=self.snapshot(oid)
        self.assertEqual(self.deliver(oid,'x'*10001).status_code,413)
        self.assertEqual(self.snapshot(oid),original)
        self.assertEqual(self.completed_events(oid),[])

    def test_global_body_guard_413_json_no_writes(self):
        rs,oid,pid,headers=self.pending()
        original=self.snapshot(oid)
        response=self.deliver(oid,'x'*(8*1024*1024+1))
        self.assertEqual(response.status_code,413)
        self.assertFalse(response.get_json()['ok'])
        self.assertEqual(self.snapshot(oid),original)
        self.assertEqual(self.completed_events(oid),[])

    def test_10000_character_boundary(self):
        rs,oid,pid,headers=self.pending()
        text='界'*10000
        self.assertEqual(self.deliver(oid,text).status_code,200)
        self.assertEqual(self.read(oid,headers)['items'],[text])

    def test_no_owner_no_access(self):
        rs,oid,pid,headers=self.pending()
        response=self.deliver(oid,client=self.fx.fx._owner(role='admin'))
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.completed_events(oid),[])

    def test_unauthenticated_and_csrf_denied(self):
        import web_admin.app as wa
        rs,oid,pid,headers=self.pending()
        response=wa.app.test_client().post(f'/api/orders/{oid}/deliver',json={'delivery':'x'})
        self.assertIn(response.status_code,(401,403))
        self.assertEqual(self.owner.post(f'/api/orders/{oid}/deliver',json={'delivery':'x'}).status_code,403)
        self.assertEqual(self.completed_events(oid),[])

    def test_customer_unchanged(self):
        db.cur.execute("INSERT INTO orders(user_id,product_name,price,delivery) VALUES(5,'customer',10,'customer original')")
        db.conn.commit();oid=db.cur.lastrowid
        original=self.snapshot(oid)
        self.assertEqual(self.deliver(oid).status_code,400)
        self.assertEqual(self.snapshot(oid),original)

    def test_unknown_order_404(self):
        self.assertEqual(self.deliver(99999999).status_code,404)

    def test_missing_encryption_key_fails_closed(self):
        rs,oid,pid,headers=self.pending()
        original=self.snapshot(oid)
        with patch.dict(os.environ,{'DATA_ENCRYPTION_KEY':''}):
            response=self.deliver(oid,'must not store plaintext')
            self.assertEqual(response.status_code,503)
        self.assertEqual(self.snapshot(oid),original)
        self.assertEqual(self.completed_events(oid),[])

    def test_outbox_failure_rolls_back_delivery_status_timestamp(self):
        rs,oid,pid,headers=self.pending()
        original=self.snapshot(oid)
        real=outbox.emit_event
        def failure(*args,**kwargs):
            if args[2]=='delivery.completed':raise RuntimeError('synthetic outbox failure')
            return real(*args,**kwargs)
        with patch.object(outbox,'emit_event',side_effect=failure):
            response=self.deliver(oid,'rollback secret')
            self.assertEqual(response.status_code,500)
        self.assertEqual(self.snapshot(oid),original)
        self.assertEqual(self.completed_events(oid),[])
        self.assertEqual(self.read(oid,headers)['delivery_status'],'pending')
        self.assertEqual(self.deliver(oid,'successful retry').status_code,200)
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_crash_after_delivery_write_does_not_commit_helper(self):
        rs,oid,pid,headers=self.pending()
        original=self.snapshot(oid);real=idem.safe_write_delivery
        def crash(*args,**kwargs):
            self.assertEqual(real(*args,**kwargs),'written')
            raise RuntimeError('synthetic crash after delivery write')
        with patch.object(idem,'safe_write_delivery',side_effect=crash):
            self.assertEqual(self.deliver(oid).status_code,500)
        self.assertEqual(self.snapshot(oid),original)
        self.assertEqual(self.completed_events(oid),[])
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_safe_write_standalone_replay_and_missing_order(self):
        rs,oid,pid,headers=self.pending()
        encrypted=encrypt_text('standalone writer')
        self.assertEqual(idem.safe_write_delivery(oid,encrypted),'written')
        self.assertEqual(idem.safe_write_delivery(oid,encrypt_text('do not overwrite')),'already_set')
        self.assertEqual(decrypt_text(self.snapshot(oid)[0]),'standalone writer')
        self.assertEqual(idem.safe_write_delivery(99999999,encrypted),'order_not_found')

    def test_safe_write_joins_outer_rollback(self):
        rs,oid,pid,headers=self.pending()
        original=self.snapshot(oid)
        with self.assertRaises(RuntimeError):
            with atomic(db.get_conn()):
                self.assertEqual(idem.safe_write_delivery(oid,encrypt_text('rolled back')),'written')
                raise RuntimeError('outer rollback')
        self.assertEqual(self.snapshot(oid),original)

    def test_helper_cannot_write_terminal_order(self):
        rs,oid,pid,headers=self.pending()
        self.fx.cancel(oid)
        original=self.snapshot(oid)
        self.assertEqual(idem.safe_write_delivery(oid,encrypt_text('no')),'terminal')
        self.assertEqual(self.snapshot(oid),original)

    def test_concurrent_deliveries_one_value_one_event(self):
        rs,oid,pid,headers=self.pending()
        barrier=threading.Barrier(2);responses=[];errors=[]
        def run(content):
            try:
                owner=self.fx.fx._owner()
                barrier.wait(timeout=10)
                response=self.deliver(oid,content,client=owner)
                responses.append((content,response.status_code,response.get_json()))
            except Exception as exc:errors.append(type(exc).__name__)
            finally:db.close_thread_connection()
        threads=[threading.Thread(target=run,args=(s,)) for s in ('race first','race second')]
        for t in threads:t.start()
        for t in threads:t.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads));self.assertEqual(errors,[])
        self.assertEqual(len(responses),2)
        self.assertTrue(any(status==200 for _,status,_ in responses),responses)
        self.assertTrue(all(status in (200,409) for _,status,_ in responses),responses)
        stored=decrypt_text(self.snapshot(oid)[0])
        self.assertIn(stored,('race first','race second'))
        for content,status,body in responses:
            if status==200 and content!=stored:self.assertEqual(body['note'],'delivery already present')
        self.assertEqual(len(self.completed_events(oid)),1)
        self.assertEqual(self.read(oid,headers)['items'],[stored])
        response=self.deliver(oid,'post-race overwrite')
        self.assertEqual(response.get_json()['note'],'delivery already present')
        self.assertEqual(decrypt_text(self.snapshot(oid)[0]),stored)
        self.assertEqual(reconcile_reseller(rs['id']),[])

    def test_idor_foreign_reseller_cannot_read_delivery(self):
        rs,oid,pid,headers=self.pending()
        other,other_oid,other_pid,foreign_headers=self.pending()
        self.assertEqual(self.deliver(oid,'isolated delivery').status_code,200)
        response=self.api.get(f'/api/v1/orders/ORD-{oid:08d}/delivery',headers=foreign_headers)
        self.assertEqual(response.status_code,404)
        self.assertNotIn('isolated delivery',json.dumps(response.get_json()))

    def test_code_stock_creation_delivery_not_overwritten(self):
        rs,oid,pid=self.fx.fx._make_reseller_order()
        key,secret=rdb.create_api_key(rs['id'],'test')
        headers=fixtures.support._hdr(key,secret)
        original=self.snapshot(oid)
        items=self.read(oid,headers)['items']
        self.assertTrue(items)
        events=self.completed_events(oid)
        self.assertEqual(len(events),1)
        response=self.deliver(oid,'not allowed to replace code stock')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['note'],'delivery already present')
        self.assertEqual(self.snapshot(oid),original)
        self.assertEqual(self.completed_events(oid),events)

    def test_static_manual_product_delivery_unchanged(self):
        rs,oid,pid,headers=self.pending()
        # The existing creation path uses static text when configured.
        db.cur.execute('UPDATE products SET delivery=? WHERE id=?',('static product delivery',pid))
        db.conn.commit()
        key,secret=rdb.create_api_key(rs['id'],'test')
        response=self.api.post('/api/v1/orders',headers=fixtures.support._hdr(key,secret),
                              json={'product_id':rdb.ensure_product_public_id(pid),'quantity':1,'client_order_id':'static-followup'})
        self.assertEqual(response.status_code,201,response.get_json())
        new_oid=int(response.get_json()['data']['order_id'].replace('ORD-',''))
        self.assertEqual(self.read(new_oid,headers)['items'],['static product delivery'])
        self.assertEqual(len(self.completed_events(new_oid)),1)

    def test_payload_allowlist_and_log_hygiene(self):
        rs,oid,pid,headers=self.pending()
        secret='PRIVATE-DELIVERY-NEVER-IN-LOGS'
        response=self.deliver(oid,secret)
        self.assertEqual(response.status_code,200)
        events=self.completed_events(oid)
        expected={'event','event_type','order_id','reseller_id','product_id','quantity',
                  'order_status','delivery_status','timestamp','created_at','note'}
        self.assertEqual(set(events[0]),expected)
        self.assertNotIn(secret,json.dumps(events))
        self.assertNotIn(secret,json.dumps(response.get_json()))
        for table in ('admin_logs','reseller_security_events','reseller_api_logs','webhook_outbox'):
            db.cur.execute(f'SELECT * FROM {table}')
            self.assertNotIn(secret,str(db.cur.fetchall()))

    def test_ui_server_eligibility_updated_after_delivery(self):
        rs,oid,pid,headers=self.pending()
        rows={r['id']:r for r in self.owner.get('/api/orders?per_page=100').get_json()['orders']}
        self.assertTrue(rows[oid]['can_deliver'])
        self.deliver(oid)
        rows={r['id']:r for r in self.owner.get('/api/orders?per_page=100').get_json()['orders']}
        self.assertFalse(rows[oid]['can_deliver'])
        js=(ROOT/'web_admin/static/js/app.js').read_text()
        self.assertIn('function openConfirm(',js)
        self.assertIn('function deliverOrderModal(',js)
        self.assertIn('o.can_deliver && !o.has_delivery',js)
        self.assertIn('maxlength="10000"',js)

    def test_outbox_dedup_constraint_preserved(self):
        rs,oid,pid,headers=self.pending()
        self.deliver(oid)
        existing=self.completed_events(oid)[0]
        with atomic(db.get_conn()):
            row=outbox.emit_event_atomic(rs['id'],'delivery.completed',f'ORD-{oid:08d}',existing,raise_on_error=True)
        self.assertIsNone(row)
        self.assertEqual(len(self.completed_events(oid)),1)


if __name__=='__main__':
    unittest.main()
