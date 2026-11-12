"""Continuous-control wire format and keyboard state, independent of Qt and transport."""
from dataclasses import dataclass, field
import math
import struct

COMMANDS = dict(continuous_caps=28, continuous_target=29, continuous_arm=2)
CAPS = struct.Struct('<BBHHH4f')
TARGET = struct.Struct('<BBHIIff')
STATE = struct.Struct('<4I7f2h2H')
REPLIES = {0x99: struct.Struct('<24s'), 0x9a: struct.Struct('<52s')}
UPDATE_SECONDS = .5
LEASE_MS = 2000
ACK_SECONDS = .6
# Maximum outer-loop wheel-speed difference (0.6m/s) / wheel track (0.16m).
OMEGA_LIMIT = 3.75


def encode(command, values):
    if command == 'continuous_arm':
        return b'\x06'
    if command == 'continuous_caps':
        return b''
    v, w = values['v'], values['omega']
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (v, w)):
        raise ValueError('连续目标必须是有限数')
    if abs(v) > 1.18 + 1e-7 or abs(w) > OMEGA_LIMIT + 1e-7:
        raise ValueError('连续目标超过能力')
    for key in ('revision', 'deadline_tick'):
        if type(values[key]) is not int or not 0 <= values[key] <= 0xffffffff:
            raise ValueError('配置版本或截止时间无效')
    flags = values.get('flags', 0)
    if type(flags) is not int or flags not in (0, 1):
        raise ValueError('连续目标标志无效')
    return TARGET.pack(2, flags, 0, values['revision'], values['deadline_tick'], v, w)


@dataclass(frozen=True)
class DrivingEvent:
    source: str
    sequence: int
    generated: float
    command: str


@dataclass
class DiscreteDrivingState:
    """离散事件只更新意图；松键不改变两条方向轴。"""
    base_v: float = .1
    base_omega: float = .3
    drive: int = 0
    turn: int = 0
    speed_limit: float = 1.18
    omega_limit: float = OMEGA_LIMIT
    held: set = field(default_factory=set)

    def clear(self):
        self.drive = self.turn = 0
        self.held.clear()

    def apply(self, commands):
        commands = list(commands)
        for positive, negative, axis in (('up', 'down', 'drive'), ('left', 'right', 'turn')):
            if positive in commands and negative in commands:
                setattr(self, axis, 0)
            elif positive in commands or negative in commands:
                direction = 1 if positive in commands else -1
                previous = getattr(self, axis)
                setattr(self, axis, 0 if previous == -direction else direction)
        for up, down, attr, step, limit in (('w', 's', 'base_v', .05, self.speed_limit),
                                           ('d', 'a', 'base_omega', .1, self.omega_limit)):
            self_value = min(limit, max(0., getattr(self, attr)))
            change = (commands.count(up) - commands.count(down)) * step
            setattr(self, attr, min(limit, max(0., round(self_value + change, 10))))

    def poll(self, now):
        pass

    def target(self, speed_limit=None, omega_limit=None):
        speed_limit = self.speed_limit if speed_limit is None else speed_limit
        omega_limit = self.omega_limit if omega_limit is None else omega_limit
        v = self.drive * min(self.base_v, speed_limit)
        w = self.turn * min(self.base_omega, omega_limit)
        return v, -w if v < 0 else w


@dataclass
class KeyboardState:
    base_v: float = .1
    base_omega: float = .3
    multiplier: float = 1.
    held: set = field(default_factory=set)
    repeat_due: float = 0.
    speed_limit: float = 1.18
    omega_limit: float = OMEGA_LIMIT

    def clear(self):
        self.held.clear()
        self.repeat_due = 0.

    def press(self, key, now):
        if key in self.held:
            return
        self.held.add(key)
        self.clamp_multiplier()
        if key in ('z', 'x'):
            self.adjust()
            self.repeat_due = now + .2

    def release(self, key, now):
        self.held.discard(key)
        if key in ('z', 'x'):
            self.repeat_due = now + .2

    def adjust(self):
        if ('z' in self.held) != ('x' in self.held):
            self.clamp_multiplier()
            self.multiplier = min(10., max(.1, self.multiplier * (1.1 if 'z' in self.held else .9)))
            self.clamp_multiplier()

    def multiplier_limit(self):
        maximum = 10.
        if ('up' in self.held) != ('down' in self.held):
            maximum = min(maximum, self.speed_limit / self.base_v)
        if ('left' in self.held) != ('right' in self.held):
            maximum = min(maximum, self.omega_limit / self.base_omega)
        return maximum

    def clamp_multiplier(self):
        # Store the reachable multiplier, so one X press always lowers a capped target.
        self.multiplier = min(self.multiplier, self.multiplier_limit())

    def poll(self, now):
        if self.held.intersection(('z', 'x')) and now >= self.repeat_due:
            self.adjust()
            self.repeat_due = now + .2  # No catch-up burst after a stalled event loop.

    def target(self, speed_limit=None, omega_limit=None):
        speed_limit = self.speed_limit if speed_limit is None else speed_limit
        omega_limit = self.omega_limit if omega_limit is None else omega_limit
        v = (int('up' in self.held) - int('down' in self.held)) * self.base_v * self.multiplier
        w = (int('left' in self.held) - int('right' in self.held)) * self.base_omega * self.multiplier
        # Steering names the travel path: reversing left needs clockwise body yaw.
        # With no translation, retain the independent in-place left/right controls.
        if v < 0:
            w = -w
        factor = max(1., abs(v) / speed_limit, abs(w) / omega_limit)
        return v / factor, w / factor
