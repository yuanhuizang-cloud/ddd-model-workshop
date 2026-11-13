"""SQLite 教学适配器：聚合状态、幂等结果、outbox 同事务提交。
未实现发件器/消费者；持久化不等于消息已送达。
"""
import json
import sqlite3
from model import Book, Voyage, Conflict, RuleViolation


def connect(path):
    c = sqlite3.connect(path, timeout=5, isolation_level=None)
    c.execute('PRAGMA busy_timeout=5000')
    return c


def initialize(path):
    with connect(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS voyages (
          id TEXT PRIMARY KEY, capacity INTEGER NOT NULL CHECK(capacity>=0),
          reserved INTEGER NOT NULL CHECK(reserved>=0 AND reserved<=capacity),
          version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS commands (
          id TEXT PRIMARY KEY, voyage_id TEXT NOT NULL,
          slots INTEGER NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        ''')
        c.execute('INSERT OR IGNORE INTO voyages VALUES (?, ?, ?, ?)',
                  ('V001', 10, 0, 0))


def load(c, voyage_id):
    row = c.execute('SELECT id, capacity, reserved, version FROM voyages WHERE id=?',
                    (voyage_id,)).fetchone()
    if row is None:
        raise RuleViolation('航次不存在')
    return Voyage(*row)


def commit(c, voyage, expected_version, cmd, before_commit=lambda: None):
    c.execute('BEGIN IMMEDIATE')
    try:
        old = c.execute('SELECT voyage_id, slots, result FROM commands WHERE id=?',
                        (cmd.command_id,)).fetchone()
        if old:
            if old[:2] != (cmd.voyage_id, cmd.slots):
                raise RuleViolation('幂等键复用但参数不同')
            c.commit()
            return json.loads(old[2])
        updated = c.execute('''UPDATE voyages SET reserved=?, version=version+1
          WHERE id=? AND version=?''',
          (voyage.reserved, voyage.id, expected_version)).rowcount
        if updated != 1:
            raise Conflict('并发版本冲突')
        result = {'event_id': cmd.command_id, 'type': 'SlotsReserved',
                  'voyage_id': voyage.id, 'slots': cmd.slots,
                  'reserved': voyage.reserved, 'version': expected_version + 1}
        payload = json.dumps(result, ensure_ascii=False)
        c.execute('INSERT INTO commands VALUES (?, ?, ?, ?)',
                  (cmd.command_id, cmd.voyage_id, cmd.slots, payload))
        c.execute('INSERT INTO outbox VALUES (?, ?)', (cmd.command_id, payload))
        before_commit()  # 测试在提交前注入故障，验证三个写入全部回滚
        c.commit()
        return result
    except BaseException:
        c.rollback()
        raise


def book(path, cmd: Book, before_commit=lambda: None):
    cmd.validate()
    c = connect(path)
    try:
        for _ in range(5):
            # 重放必须先于领域验证，否则售罄后重试已成功命令会被错误拒绝。
            old = c.execute('SELECT voyage_id, slots, result FROM commands WHERE id=?',
                            (cmd.command_id,)).fetchone()
            if old:
                if old[:2] != (cmd.voyage_id, cmd.slots):
                    raise RuleViolation('幂等键复用但参数不同')
                return json.loads(old[2])
            v = load(c, cmd.voyage_id)
            version = v.version
            try:
                v.reserve(cmd.slots)
            except RuleViolation:
                # 同一命令可能刚被另一个请求提交，先重新确认幂等结果。
                old = c.execute('SELECT voyage_id, slots, result FROM commands WHERE id=?',
                                (cmd.command_id,)).fetchone()
                if old and old[:2] == (cmd.voyage_id, cmd.slots):
                    return json.loads(old[2])
                raise
            try:
                return commit(c, v, version, cmd, before_commit)
            except Conflict:
                continue  # 重新加载并重新校验规则，禁止盲目重放旧快照
        raise Conflict('重试耗尽；调用方应退避或返回可重试错误')
    finally:
        c.close()
