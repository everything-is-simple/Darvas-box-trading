# -*- coding: utf-8 -*-
def markov(sizes, p, W, L):
    K = len(sizes)
    P = [[0.0]*K for _ in range(K)]
    for i in range(K):
        P[i][min(i+1, K-1)] += p
        P[i][max(i-1, 0)] += (1-p)
    pi = [1.0/K]*K
    for _ in range(2000):
        new = [sum(pi[i]*P[i][j] for i in range(K)) for j in range(K)]
        if max(abs(new[j]-pi[j]) for j in range(K)) < 1e-12:
            pi = new; break
        pi = new
    epu = p*W - (1-p)*L
    Es = sum(pi[i]*sizes[i] for i in range(K))
    return pi, Es, Es*epu, -L*max(sizes), pi[-1]

LADDERS = {
    '0017现行 1->5     ': [1,5],
    'A 1-2-3-4-5       ': [1,2,3,4,5],
    'B 1-2-3-5         ': [1,2,3,5],
    'C 1-2-2-3-5       ': [1,2,2,3,5],
    'D 2-2-3-3-5(你念的)': [2,2,3,3,5],
    'E 1-2-3-3-5       ': [1,2,3,3,5],
    'FLAT 恒定1不加     ': [1],
}

for (p,W,L,tag) in [(0.30,1.0,1.0,'p=0.30 W=1 L=1 (对称盈亏口径)'),
                    (0.30,2.0,0.5,'p=0.30 W=2 L=0.5 (紧止损口径)'),
                    (0.35,2.0,0.5,'p=0.35 W=2 L=0.5'),
                    (0.30,2.0,1.0,'p=0.30 W=2 L=1.0 (止损不收紧)')]:
    print(f'\n=== {tag} ===')
    print(f'{"梯子":<22}{"E[仓位]":>8}{"E/笔(R)":>9}{"最差单笔":>9}{"P(顶档)":>9}  稳态分布')
    for name, sz in LADDERS.items():
        pi, Es, Et, worst, ptop = markov(sz, p, W, L)
        dist = ' '.join(f'{x:.0%}' for x in pi)
        print(f'{name:<22}{Es:>8.2f}{Et:>9.3f}{worst:>9.2f}{ptop:>9.1%}  [{dist}]')

print('\n=== 用户恐惧路径复核 (W=1 L=1) ===')
print('0017 [1,5]: 赢(1u) -> 输(5u) -> 输(1u): 概率 0.3*0.7*0.7=14.7%, 盈亏 +1-5-1 = -5')
print('平滑[1,2,3,4,5]: 赢(1u)->输(2u)->输(1u): 概率同 14.7%, 盈亏 +1-2-1 = -2')

print('\n=== 顶档风险对比 (p=0.3) ===')
for name, sz in [('[1,5]',[1,5]), ('[1,2,3,4,5]',[1,2,3,4,5])]:
    pi, *_ = markov(sz, 0.3, 1.0, 1.0)
    print(f'{name}: 顶档概率 {pi[-1]:.1%}, 顶档单笔亏 {max(sz)}, 顶档两连亏密度 {pi[-1]*0.49:.1%}')

# 黑名单的机会成本: 0017 底部输=冷却12月
# 每100笔交易中, 底部输的占比 -> 这些票被冻结
print('\n=== 0017 黑名单冻结频率 ===')
pi, *_ = markov([1,5], 0.3, 1.0, 1.0)
print(f'处于normal档概率 {pi[0]:.1%}, 每笔在normal输的概率 {pi[0]*0.7:.1%} -> 每100笔约{pi[0]*0.7*100:.0f}次触发12月冷却')
