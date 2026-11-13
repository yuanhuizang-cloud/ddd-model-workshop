"""DDD 教学：领域对象不依赖数据库。单位是不可拆分的舱位。"""
from dataclasses import dataclass


class RuleViolation(ValueError):
    pass


class Conflict(Exception):
    pass


@dataclass(frozen=True)
class Book:
    command_id: str
    voyage_id: str
    slots: int

    def validate(self):
        if not self.command_id or not self.voyage_id:
            raise RuleViolation('命令 ID 和航次 ID 不能为空')
        if type(self.slots) is not int or self.slots <= 0:
            raise RuleViolation('舱位必须是正整数')


@dataclass
class Voyage:
    id: str
    capacity: int
    reserved: int = 0
    version: int = 0

    def __post_init__(self):
        if (type(self.capacity) is not int or type(self.reserved) is not int
                or self.capacity < 0 or not 0 <= self.reserved <= self.capacity):
            raise RuleViolation('无效航次状态')

    def reserve(self, slots: int):
        if type(slots) is not int or slots <= 0:
            raise RuleViolation('舱位必须是正整数')
        if slots > self.capacity - self.reserved:
            raise RuleViolation('舱位不足')
        self.reserved += slots
