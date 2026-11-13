import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from lifecycle import (Command, Rejected, connect, initialize, execute,
                       consume, relay_once, expire_due)
from contextlib import closing


def fail():
    raise RuntimeError('模拟进程在关键窗口崩溃')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name)/'booking.db')
        self.consumer = str(Path(self.temp.name)/'consumer.db')
        initialize(self.db, 10)
        initialize(self.consumer)

    def tearDown(self):
        self.temp.cleanup()

    def query(self, sql, db=None):
        with closing(connect(db or self.db)) as c:
            return c.execute(sql).fetchall()

    def hold(self, rid='R1', slots=4, deadline=100):
        return execute(self.db,Command('hold:'+rid,'hold',rid,slots,deadline),0)

    def test_confirm_cancel_releases_once(self):
        self.hold()
        execute(self.db,Command('confirm','confirm','R1'),50)
        execute(self.db,Command('cancel','cancel','R1'),60)
        again = execute(self.db,Command('cancel-again','cancel','R1'),61)
        self.assertFalse(again['changed'])
        self.assertEqual(self.query('SELECT reserved FROM capacity'),[(0,)])
        self.assertEqual(self.query('SELECT count(*) FROM events'),[(3,)])

    def test_replay_precedes_capacity_and_clock_validation(self):
        cmd = Command('hold','hold','R1',10,100)
        first = execute(self.db,cmd,0)
        self.assertEqual(execute(self.db,cmd,200),first)
        with self.assertRaises(Rejected):
            execute(self.db,Command('hold','hold','R1',9,100),0)

    def test_late_confirmation_rejected_even_without_scheduler(self):
        self.hold()
        with self.assertRaises(Rejected):
            execute(self.db,Command('confirm','confirm','R1'),100)
        self.assertEqual(expire_due(self.db,100),1)
        self.assertEqual(expire_due(self.db,100),0)
        self.assertEqual(self.query('SELECT reserved FROM capacity'),[(0,)])

    def test_confirmed_is_not_expired(self):
        self.hold()
        execute(self.db,Command('confirm','confirm','R1'),99)
        execute(self.db,Command('expire','expire','R1'),100)
        self.assertEqual(self.query('SELECT state FROM reservations'),[('CONFIRMED',)])
        self.assertEqual(self.query('SELECT reserved FROM capacity'),[(4,)])

    def test_atomic_rollback(self):
        with self.assertRaises(RuntimeError):
            execute(self.db,Command('hold','hold','R1',5,100),0,fail)
        for table in ('reservations','receipts','events'):
            self.assertEqual(self.query('SELECT count(*) FROM '+table),[(0,)])
        self.assertEqual(self.query('SELECT reserved FROM capacity'),[(0,)])

    def test_capacity_under_concurrent_different_intents(self):
        def attempt(i):
            try:
                self.hold(str(i),1)
                return True
            except Rejected:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(attempt, range(20)))
        self.assertEqual(sum(results),10)
        self.assertEqual(self.query('SELECT reserved FROM capacity'),[(10,)])

    def test_concurrent_same_intent(self):
        cmd=Command('same','hold','R1',4,100)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:execute(self.db,cmd,0),range(12)))
        self.assertTrue(all(x==results[0] for x in results))
        self.assertEqual(self.query('SELECT reserved FROM capacity'),[(4,)])
        self.assertEqual(self.query('SELECT count(*) FROM events'),[(1,)])

    def test_expiry_racing_confirmation_at_deadline(self):
        self.hold()
        def call(action):
            try:
                return execute(self.db,Command(action,action,'R1'),100)['state']
            except Rejected:
                return 'REJECTED'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(call,['confirm','expire']))
        self.assertEqual(outcomes,['REJECTED','EXPIRED'])
        self.assertEqual(self.query('SELECT reserved FROM capacity'),[(0,)])

    def test_send_then_crash_replays_without_duplicate_business_effect(self):
        self.hold()
        deliver=lambda e:consume(self.consumer,e)
        with self.assertRaises(RuntimeError):
            relay_once(self.db,deliver,fail)
        self.assertEqual(self.query('SELECT sent FROM events'),[(0,)])
        self.assertEqual(relay_once(self.db,deliver),1)
        self.assertEqual(self.query('SELECT count(*) FROM inbox',self.consumer),[(1,)])
        self.assertEqual(self.query('SELECT version,state FROM projection',self.consumer),[(1,'ACTIVE')])

    def test_inbox_and_projection_rollback_together(self):
        self.hold()
        e=json.loads(self.query('SELECT payload FROM events')[0][0])
        with self.assertRaises(RuntimeError):
            consume(self.consumer,e,before_commit=fail)
        self.assertEqual(self.query('SELECT count(*) FROM inbox',self.consumer),[(0,)])
        self.assertEqual(self.query('SELECT count(*) FROM projection',self.consumer),[(0,)])
        self.assertTrue(consume(self.consumer,e))

    def test_out_of_order_snapshots_keep_newer_state(self):
        self.hold()
        execute(self.db,Command('cancel','cancel','R1'),50)
        events=[json.loads(r[0]) for r in self.query('SELECT payload FROM events ORDER BY seq DESC')]
        for e in events:
            consume(self.consumer,e)
        self.assertEqual(self.query('SELECT version,state FROM projection',self.consumer),[(2,'CANCELLED')])

    def test_event_identity_must_not_change_payload(self):
        self.hold()
        e=json.loads(self.query('SELECT payload FROM events')[0][0])
        consume(self.consumer,e)
        with self.assertRaises(Rejected):
            consume(self.consumer,dict(e,state='CANCELLED'))

    def test_invalid_quantity_and_cancelled_cannot_confirm(self):
        for slots in [0,-1,True,1.5]:
            with self.assertRaises(Rejected):
                self.hold('bad',slots)
        self.hold()
        execute(self.db,Command('cancel','cancel','R1'),50)
        with self.assertRaises(Rejected):
            execute(self.db,Command('confirm','confirm','R1'),60)


if __name__ == '__main__':
    unittest.main()
