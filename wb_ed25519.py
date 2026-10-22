"""纯标准库 Ed25519（RFC 8032）。

Freebuff 的聊天 / 会话请求会带一组设备签名头：

    x-freebuff-device-key / x-freebuff-device-ts / x-freebuff-device-sig

CLI 用 WebCrypto 生成 Ed25519 密钥并签名；网关这里只依赖标准库，所以按
RFC 8032 实现最小的一套：生成密钥、导出原始 32 字节公钥、对任意字节串
签名。曲线运算使用扩展坐标（extended coordinates），255 位标量乘在
CPython 里是毫秒级，足够一次聊天请求用。

这里**不读取**本机 Freebuff CLI 的 ``device-key.json``；网关为每个
Freebuff 账号生成自己的设备密钥，并保存在账号池自己的状态里。
"""

import hashlib
import os

#: 2^255 - 19
P = 2 ** 255 - 19
#: 群阶 L = 2^252 + 27742317777372353535851937790883648493
L = 2 ** 252 + 27742317777372353535851937790883648493
#: 曲线参数 d = -121665 / 121666 mod P
D = (-121665 * pow(121666, P - 2, P)) % P
#: 2*d，扩展坐标加法里直接用
D2 = (2 * D) % P
#: sqrt(-1) mod P
I = pow(2, (P - 1) // 4, P)


def _inv(value):
    return pow(value % P, P - 2, P)


def _xrecover(y):
    """由 y 恢复 x（取偶数的那一支，和 RFC 8032 的基点一致）。"""
    y = y % P
    xx = ((y * y - 1) * _inv(D * y * y + 1)) % P
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P != 0:
        x = (x * I) % P
    if x & 1:
        x = P - x
    return x


_BY = (4 * _inv(5)) % P
_BX = _xrecover(_BY)
#: 基点，扩展坐标 (X, Y, Z, T)，其中 x = X/Z, y = Y/Z, T = XY/Z。
B = (_BX, _BY, 1, (_BX * _BY) % P)


def _add(p1, p2):
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = ((y1 - x1) * (y2 - x2)) % P
    b = ((y1 + x1) * (y2 + x2)) % P
    c = (t1 * D2 * t2) % P
    d = (z1 * 2 * z2) % P
    e = (b - a) % P
    f = (d - c) % P
    g = (d + c) % P
    h = (b + a) % P
    return ((e * f) % P, (g * h) % P, (f * g) % P, (e * h) % P)


def _double(p1):
    x1, y1, z1, _t1 = p1
    a = (x1 * x1) % P
    b = (y1 * y1) % P
    c = (2 * z1 * z1) % P
    d = (-a) % P
    e = ((x1 + y1) * (x1 + y1) - a - b) % P
    g = (d + b) % P
    f = (g - c) % P
    h = (d - b) % P
    return ((e * f) % P, (g * h) % P, (f * g) % P, (e * h) % P)


def _scalar_mult(point, scalar):
    result = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _double(point)
        scalar >>= 1
    return result


def _encode(point):
    x, y, z, _t = point
    zi = _inv(z)
    x = (x * zi) % P
    y = (y * zi) % P
    # y 的低 255 位 + x 的最低位放在最高位，小端 32 字节。
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _clamp(digest32):
    value = int.from_bytes(digest32, "little")
    value &= (1 << 254) - 8
    value |= 1 << 254
    return value


def generate():
    """生成一对 Ed25519 密钥，返回 ``(seed, public_key)``。"""
    seed = os.urandom(32)
    return seed, public_key(seed)


def public_key(seed):
    """由 32 字节私钥种子算原始公钥（32 字节）。"""
    seed = bytes(seed or b"")
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = _clamp(digest[:32])
    return _encode(_scalar_mult(B, scalar))


def sign(seed, message, public=None):
    """对 ``message`` 签名，返回 64 字节 ``R || S``。"""
    seed = bytes(seed or b"")
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    message = bytes(message or b"")
    digest = hashlib.sha512(seed).digest()
    scalar = _clamp(digest[:32])
    prefix = digest[32:]
    pub = bytes(public) if public is not None else _encode(
        _scalar_mult(B, scalar))
    if len(pub) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(),
                       "little") % L
    r_encoded = _encode(_scalar_mult(B, r))
    k = int.from_bytes(hashlib.sha512(r_encoded + pub + message).digest(),
                       "little") % L
    s = (r + k * scalar) % L
    return r_encoded + s.to_bytes(32, "little")
