# 监控池 API 对接说明（前端）

Base URL: `http://118.194.233.95:8000`
Swagger 文档: `/docs`

## 这是什么

选股系统之外的**第二条观测线**，和 `/api/picks` 完全独立、互不影响。

盯的是**「低位首板」**这一种形态：一只票在低位（距过去 120 个交易日最低收盘价涨幅 ≤ 30%）、且此前 60 个交易日没涨停过，突然拉出一个涨停板——这种票会被自动放进监控池，此后 30 个交易日内每天记录它的量价演化，直到它**再次涨停**（命中）或**窗口走完**（到期）。

为什么盯这个：实测 892 个交易日全历史数据，这类票 30 日内再次涨停的概率是 **32.6%**，而随机挑一个非涨停日只有 **19.9%**。

数据每个交易日 18:30 盘后自动更新。

---

## 三个接口

### 1. `GET /api/watch` — 池子列表

```
GET /api/watch?status=watching&limit=50
```

| 参数 | 说明 |
|---|---|
| `status` | `watching` 跟踪中 / `hit` 已再次涨停 / `expired` 30日窗口走完未涨停。不传=全部 |
| `entry_type` | `solo` 孤板 / `consecutive` 连板。**连板的历史命中率是孤板的两倍**（70.8% vs 34.8%） |
| `board_group` | `main` 主板 / `other` 非主板 |
| `exclude_broke` | `true` = 剔除已跌破首板日开盘价的票。这批票命中率只有 34%，未跌破的有 51%。**默认 false** |
| `since` | 只看首板日 ≥ 该日期，如 `2026-08-01` |
| `order_by` | `live_score`(默认) / `entry_score` / `trigger_date` |
| `limit` | 1~500，默认 100 |

**返回示例**（单条）：

```json
{
  "id": 1460,
  "code": "603207",
  "name": "小方制药",
  "board_group": "main",

  "trigger_date": "2026-09-01",      // 首板日
  "trigger_close": 25.4,             // 首板日收盘价
  "trigger_pct": 10.0043,            // 首板日涨幅%
  "gain_from_low": 18.1395,          // 距120日低点涨幅%,越小越低位

  "trigger_vol_ratio": 2.0466,       // 首板日放量倍数 ★重要,见下
  "flat_days": 91,                   // 低位横盘天数(仅展示,无预测力)

  "entry_score": 0.874,              // 入池评分 0~1
  "entry_scores": {                  // 评分分项,可做雷达图/明细展示
    "trigger_volume": 0.9922,
    "low_position": 0.593,
    "liquidity": 1.0
  },
  "live_score": 0.7844,              // 跟踪评分 0~1,默认排序字段

  "consec_boards": 2,                // 首板起连了几个板(1=孤板)
  "entry_type": "consecutive",
  "broke_open_date": null,           // 跌破首板开盘价的日期,null=未跌破
  "broke_open_days": null,

  "status": "watching",
  "hit_date": null,                  // 再次涨停日(命中时才有)
  "hit_days": null,                  // 距首板第几个交易日命中
  "expire_date": null,               // 30日窗口末日(行情未走满时为null)

  "last_ret_since": 2.5197,          // 最新一天相对首板收盘的涨跌%
  "last_amount_ratio": 1.6573,       // 最新一天成交额/首板日成交额
  "days_in_pool": 5,                 // 已跟踪几个交易日
  "track": []                        // 列表接口恒为空,详情接口才有
}
```

### 2. `GET /api/watch/stats` — 命中率统计

```json
{
  "total": 1486,
  "watching": 220,
  "hit": 558,
  "expired": 708,
  "hit_rate": 44.08,                 // ⚠️ 见下方"注意"
  "avg_hit_days": 11.75,             // 平均多少个交易日后再次涨停
  "by_entry_type": {
    "consecutive": 73.3,             // 连板组命中率%
    "solo": 39.36                    // 孤板组命中率%
  },
  "benchmark_hint": "历史基准：孤板 32.63% / 连板 67.08% / 随机 19.87%"
}
```

支持 `?since=2026-08-01` 只统计某日期之后入池的。

### 3. `GET /api/watch/{code}` — 个股详情

比列表多一个 `track` 数组，是入池后每个交易日的量价记录，**可直接用来画演化曲线**：

```json
"track": [
  {"trade_date":"2026-09-02","days_since":1,"close":27.94,"pct_chg":10.0,
   "ret_since":10.0,"amount_ratio":2.4668,"is_limit_up":true},
  {"trade_date":"2026-09-03","days_since":2,"close":25.95,"pct_chg":-7.1224,
   "ret_since":2.1654,"amount_ratio":2.7843,"is_limit_up":false},
  ...
]
```

同一只票可能多次入池（不同时期的首板），默认返回最近一次；要指定用 `?trigger_date=2026-09-01`。

---

## 展示建议

**列表页**默认这样调，拿到的就是当前最值得盯的票：

```
GET /api/watch?status=watching&order_by=live_score&limit=50
```

**几个字段的含义，值得在 UI 上体现**：

- **`trigger_vol_ratio`（首板放量倍数）是最有预测力的字段**。实测：<2倍 命中率 43.1%，2-4倍 40.2%，4-6倍 32.6%，≥6倍 只有 18.8%。**越小越好**——首板放量太夸张通常是一日游资金对倒。建议 ≥4 倍的标黄、≥6 倍标红。
- **`entry_score` vs `live_score` 的区别**：`entry_score` 只用首板当天的信息算，入池后不再变；`live_score` 会随行情更新（连板加分、跌破首板开盘价打折）。列表排序用 `live_score`，但如果要回溯"当初该不该关注它"，看 `entry_score`。
- **`broke_open_date` 不为 null** = 已跌破首板日开盘价，走弱信号，建议置灰或加标记。想直接过滤掉就传 `exclude_broke=true`。
- **`flat_days`（低位横盘天数）只做展示，别用来排序**。我们实测过，它和命中率的相关性接近于 0，放进评分只会稀释有效信号。

**详情页**用 `track` 画两条线：`ret_since`（相对首板日的涨跌幅）和 `amount_ratio`（相对首板日的成交额比），`is_limit_up=true` 的点标出来。

---

## 三个注意事项

**1. `stats.hit_rate` 偏高，不能直接当"这个策略的胜率"对外展示。**

44.08% 是 `hit/(hit+expired)`，存在幸存者偏差——新入池的票一旦命中就立刻结算进分母，没命中的还挂在 `watching` 不计入。只统计 30 日窗口完整走完的世代，真实值是 **33.9%**。如果要展示一个"可信的命中率"，用 `by_entry_type` 里的分组值，或者自己按 `trigger_date <= 今天-30个交易日` 过滤后算。

**2. `expire_date` 为 `null` 是正常的**，表示这只票的 30 日窗口还没走满（行情还没到那天），不是数据缺失。

**3. `/api/watch/{code}` 从外网访问目前返回 503**（`/api/watch` 和 `/api/watch/stats` 正常）。服务器本机直连是 200、数据完整，所以是外层代理/CDN 对这个 URL 形态的处理问题，不是接口本身的 bug。前端联调时如果卡在这里，先在服务器上 `curl 127.0.0.1:8000/api/watch/603207` 验证，再排查代理配置。
