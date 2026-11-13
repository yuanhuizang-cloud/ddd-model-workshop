"""订舱生命周期：领域状态机 + SQLite 原子用例 + 幂等 + Outbox/Inbox。
所有时间由服务端传入的整数 UTC 秒表示，测试使用逻辑时钟。
演示为单租户、单航次数据库；无真实 MQ 或付款服务。
"""
from dataclasses import dataclass, replace
import json
import sqlite3
from contextlib import closing


class Rejected(Exception):
    pass


@dataclass(frozen=True)
class Reservation:
    id: str
    slots: int
    expires_at: int
    state: str = 'ACTIVE'
    version: int = 1

    def change(self, action, now):
        """返回 (新状态, 容量释放量)。未变化的请求不产生新事实。"""
        if action == 'confirm':
            if self.state == 'CONFIRMED':
                return self, 0
            if self.state != 'ACTIVE' or now >= self.expires_at:
                raise Rejected('预留已结束或超过确认截止时间')
            return replace(self, state='CONFIRMED', version=self.version + 1), 0
        if action == 'cancel':
            if self.state in ('CANCELLED', 'EXPIRED'):
                return self, 0
            # 本场景允许已确认但尚未履约的预订取消；不涉及退款。
            return replace(self, state='CANCELLED', version=self.version + 1), self.slots
        if action == 'expire':
            if self.state != 'ACTIVE':
                return self, 0
            if now < self.expires_at:
                raise Rejected('尚未到期')
            return replace(self, state='EXPIRED', version=self.version + 1), self.slots
        raise Rejected('未知操作')


@dataclass(frozen=True)
class Command:
    command_id: str
    action: str
    reservation_id: str
    slots: int = 0
    expires_at: int = 0

    def fingerprint(self):
        if not self.command_id or not self.reservation_id:
            raise Rejected('命令和预订身份不能为空')
        if self.action not in ('hold', 'confirm', 'cancel', 'expire'):
            raise Rejected('未知操作')
        if self.action != 'hold' and (self.slots != 0 or self.expires_at != 0):
            raise Rejected('只有 hold 接受数量与截止时间')
        return json.dumps([self.action, self.reservation_id, self.slots,
                           self.expires_at], separators=(',', ':'))


def connect(path):
    return sqlite3.connect(path, timeout=10, isolation_level=None)


def initialize(path, capacity=10):
    if type(capacity) is not int or capacity < 0:
        raise Rejected('容量必须是非负整数')
    with closing(connect(path)) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS capacity (
          id INTEGER PRIMARY KEY CHECK(id=1), total INTEGER NOT NULL,
          reserved INTEGER NOT NULL CHECK(reserved>=0 AND reserved<=total));
        CREATE TABLE IF NOT EXISTS reservations (
          id TEXT PRIMARY KEY, slots INTEGER NOT NULL CHECK(slots>0),
          expires_at INTEGER NOT NULL, state TEXT NOT NULL
          CHECK(state IN ('ACTIVE','CONFIRMED','CANCELLED','EXPIRED')),
          version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS receipts (
          id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
          payload TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS inbox (
          consumer TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(consumer,event_id));
        CREATE TABLE IF NOT EXISTS projection (
          consumer TEXT NOT NULL, reservation_id TEXT NOT NULL,
          version INTEGER NOT NULL, state TEXT NOT NULL,
          PRIMARY KEY(consumer,reservation_id));
        ''')
        c.execute('INSERT OR IGNORE INTO capacity VALUES (1,?,0)', (capacity,))


def execute(path, cmd, now, before_commit=lambda: None):
    """BEGIN IMMEDIATE 先取得写权限，串行化本 SQLite 数据库写入。
    不能把这种锁粒度当成生产关系数据库的通用结论。
    """
    fingerprint = cmd.fingerprint()
    if type(now) is not int:
        raise Rejected('服务端时间必须为整数秒')
    with closing(connect(path)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            old = c.execute('SELECT fingerprint,result FROM receipts WHERE id=?',
                            (cmd.command_id,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise Rejected('同一幂等键对应不同业务参数')
                c.commit()
                return json.loads(old[1])
            row = c.execute('SELECT id,slots,expires_at,state,version '
                            'FROM reservations WHERE id=?',
                            (cmd.reservation_id,)).fetchone()
            if cmd.action == 'hold':
                if row:
                    raise Rejected('预订身份已存在')
                if type(cmd.slots) is not int or cmd.slots <= 0:
                    raise Rejected('数量必须为正整数')
                if type(cmd.expires_at) is not int or cmd.expires_at <= now:
                    raise Rejected('截止时间必须在未来')
                changed = c.execute('UPDATE capacity SET reserved=reserved+? '
                                    'WHERE id=1 AND reserved+?<=total',
                                    (cmd.slots, cmd.slots)).rowcount
                if changed != 1:
                    raise Rejected('容量不足')
                new = Reservation(cmd.reservation_id, cmd.slots, cmd.expires_at)
                c.execute('INSERT INTO reservations VALUES (?,?,?,?,?)',
                          (new.id,new.slots,new.expires_at,new.state,new.version))
            else:
                if not row:
                    raise Rejected('预订不存在')
                original = Reservation(*row)
                new, released = original.change(cmd.action, now)
                changed = new != original
                if changed:
                    c.execute('UPDATE reservations SET state=?,version=? WHERE id=?',
                              (new.state,new.version,new.id))
                    c.execute('UPDATE capacity SET reserved=reserved-? WHERE id=1',
                              (released,))
            result = {'reservation_id':new.id, 'state':new.state,
                      'version':new.version, 'changed':bool(changed)}
            if changed:
                event = {'event_id':cmd.command_id,'type':'ReservationStateChanged',
                         'schema_version':1,'reservation_id':new.id,'state':new.state,
                         'version':new.version,'occurred_at':now}
                c.execute('INSERT INTO events(event_id,payload) VALUES (?,?)',
                          (cmd.command_id,json.dumps(event,sort_keys=True)))
            c.execute('INSERT INTO receipts VALUES (?,?,?)',
                      (cmd.command_id,fingerprint,json.dumps(result)))
            before_commit()
            c.commit()
            return result
        except BaseException:
            c.rollback()
            raise


def consume(path, event, consumer='booking-screen', before_commit=lambda: None):
    """状态快照投影：版本更大才覆盖。不能用于金额增量/必须逐件处理的事件。"""
    required = {'event_id','type','schema_version','reservation_id','state','version'}
    if not required <= event.keys() or event['schema_version'] != 1:
        raise Rejected('不支持的消息契约')
    if (event['type'] != 'ReservationStateChanged'
            or event['state'] not in ('ACTIVE','CONFIRMED','CANCELLED','EXPIRED')
            or type(event['version']) is not int or event['version'] < 1
            or not event['event_id'] or not event['reservation_id']):
        raise Rejected('非法状态消息')
    payload = json.dumps(event,sort_keys=True)
    with closing(connect(path)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            old = c.execute('SELECT payload FROM inbox WHERE consumer=? AND event_id=?',
                            (consumer,event['event_id'])).fetchone()
            if old:
                if old[0] != payload:
                    raise Rejected('同一事件身份对应不同载荷')
                c.commit()
                return False
            c.execute('INSERT INTO inbox VALUES (?,?,?)',
                      (consumer,event['event_id'],payload))
            c.execute('''INSERT INTO projection VALUES (?,?,?,?)
              ON CONFLICT(consumer,reservation_id) DO UPDATE SET
              version=excluded.version,state=excluded.state
              WHERE excluded.version>projection.version''',
              (consumer,event['reservation_id'],event['version'],event['state']))
            before_commit()
            c.commit()
            return True
        except BaseException:
            c.rollback()
            raise


def relay_once(path, deliver, after_send=lambda: None):
    """单工作者示例；实际网络交付发生在数据库事务之外。
    deliver 返回表示下游已持久接收；若返回后崩溃，下次会重复发送。
    """
    with closing(connect(path)) as c:
        rows = c.execute('SELECT seq,payload FROM events WHERE sent=0 ORDER BY seq').fetchall()
    for seq, payload in rows:
        deliver(json.loads(payload))
        after_send()
        with closing(connect(path)) as c:
            c.execute('UPDATE events SET sent=1 WHERE seq=?', (seq,))
    return len(rows)


def expire_due(path, now):
    """定期可恢复扫描；执行时再次判断，查询后确认成功不会被覆盖。"""
    with closing(connect(path)) as c:
        due = c.execute("SELECT id FROM reservations WHERE state='ACTIVE' "
                        'AND expires_at<=?', (now,)).fetchall()
    for (reservation_id,) in due:
        # 在本示例中 ID 永不重用，故截止任务的业务键稳定。
        execute(path, Command('expiry:'+reservation_id,'expire',reservation_id),now)
    return len(due)
