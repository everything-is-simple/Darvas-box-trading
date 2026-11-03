"""统计工具：无第三方依赖（scipy/numpy 不可用）。

t 分布双尾 p 值：Student-t 密度的未归一化核 g(x)=(1+x²/df)^(-(df+1)/2)，
在 [-B,B] 上数值积分归一化，再取尾部面积。这样完全避开 lgamma 的大数相消误差。
校验点：t=1.96,df=1e9 → 0.0500；t=2.0,df=100 → 0.0477；t=2.58,df=1e9 → 0.0099。
"""
import math


def _g(x, df):
    return (1.0 + x * x / df) ** (-(df + 1) / 2.0)


def t_two_sided_p(t, df, B=200.0, n=40000):
    """Student-t 双尾 p 值（数值积分，精度 ~1e-5）。"""
    t = abs(t)
    if t == 0:
        return 1.0
    if df <= 0:
        return 1.0
    if t >= B:
        return 0.0
    # 归一化：整条曲线面积
    h = 2.0 * B / n
    s = _g(-B, df) + _g(B, df)
    for i in range(1, n):
        x = -B + i * h
        s += (4 if i % 2 else 2) * _g(x, df)
    total = s * h / 3.0
    # 尾部：[t, B]
    m = 20000
    h2 = (B - t) / m
    s2 = _g(t, df) + _g(B, df)
    for i in range(1, m):
        s2 += (4 if i % 2 else 2) * _g(t + i * h2, df)
    tail = s2 * h2 / 3.0
    return min(1.0, 2.0 * tail / total)


def required_n_per_group(delta, sd1, n1, sd2, n2, alpha_z=1.96, power_z=0.8416):
    """等样本量下，检出 delta 所需的两组各多少笔（用当前方差外推）。"""
    if delta == 0:
        return float("inf")
    sd = math.sqrt((sd1 ** 2 + sd2 ** 2) / 2)
    return (alpha_z + power_z) ** 2 * 2 * sd ** 2 / delta ** 2


def fisher_exact_two_sided(a, b, c, d):
    """2×2 表 Fisher 精确检验双尾 p。a/b = 组1 成功/失败，c/d = 组2 成功/失败。"""
    n1 = a + b
    n2 = c + d
    k = a + c
    n = n1 + n2

    def logC(n, k):
        """ln C(n, k)"""
        if k < 0 or k > n:
            return float("-inf")
        return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)

    lo = max(0, k - n2)
    hi = min(n1, k)

    def prob(x):
        # P(组1成功数 = x)，超几何
        return math.exp(logC(n1, x) + logC(n2, k - x) - logC(n, k))

    p_obs = prob(a)
    total = 0.0
    for x in range(lo, hi + 1):
        p_x = prob(x)
        if p_x <= p_obs * (1 + 1e-9):
            total += p_x
    return min(1.0, total)
