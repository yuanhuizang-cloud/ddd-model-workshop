import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from booking import initialize, connect, load, commit, book
from model import Book, Voyage, Conflict, RuleViolation


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'booking.sqlite')
        initialize(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_domain_boundaries(self):
        for slots in [0, -1, True, 1.5, 11]:
            with self.assertRaises(RuleViolation):
                Voyage('v', 10).reserve(slots)
        v = Voyage('v', 10)
        v.reserve(10)
        self.assertEqual(v.reserved, 10)

    def test_replay_even_when_full(self):
        cmd = Book('a', 'V001', 10)
        self.assertEqual(book(self.path, cmd), book(self.path, cmd))
        with connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)

    def test_key_mismatch(self):
        book(self.path, Book('a', 'V001', 2))
        with self.assertRaises(RuleViolation):
            book(self.path, Book('a', 'V001', 3))

    def test_stale_version(self):
        with connect(self.path) as c:
            a, b = load(c, 'V001'), load(c, 'V001')
            a.reserve(6)
            b.reserve(6)
            commit(c, a, 0, Book('a', 'V001', 6))
            with self.assertRaises(Conflict):
                commit(c, b, 0, Book('b', 'V001', 6))

    def test_atomic_rollback(self):
        def fail():
            raise RuntimeError('提交前故障')
        with self.assertRaises(RuntimeError):
            book(self.path, Book('a', 'V001', 4), fail)
        with connect(self.path) as c:
            self.assertEqual(load(c, 'V001').reserved, 0)
            for table in ['commands', 'outbox']:
                self.assertEqual(c.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_concurrent_no_overbooking(self):
        def attempt(i):
            try:
                book(self.path, Book(str(i), 'V001', 6))
                return True
            except RuleViolation:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, range(2))), 1)
        with connect(self.path) as c:
            self.assertEqual(load(c, 'V001').reserved, 6)

    def test_concurrent_duplicate(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: book(self.path, Book('same', 'V001', 10)), range(2)))
        self.assertEqual(results[0], results[1])
        with connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
