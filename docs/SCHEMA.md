# 数据库与迁移

存储是单个 SQLite 文件（`data/perpdesk.db`，WAL 模式）。选它不是图省事：
这是自托管单人工具，数据量在百万行以内，装一个数据库服务只会提高别人跑起来的门槛。

## 版本号

版本存在 **`PRAGMA user_version`**（SQLite 文件头里的 32 位整数），不是自建表。

* 跟着数据库文件走，`DROP TABLE` 删不掉它；
* 读写是事务性的——`BEGIN` 里改完 `ROLLBACK` 会退回原值，所以能和 DDL 放进同一个事务；
* `_migrations` 表只是给人看的执行日志（谁、什么时候、跑了多久），**任何判断都不读它**。

当前版本见 `backend/app/migrations.py` 的 `SCHEMA_VERSION`。

## 启动时会发生什么

```
连接 → 读 user_version
      ├─ 高于程序支持的版本 → 直接报错退出
      ├─ 等于            → 什么都不做
      └─ 低于            → 有存量数据就先备份，然后逐条迁移
```

**版本倒挂会拒绝启动。** 场景是真实的：用户 `git pull` 到新版建了库，又切回旧 tag——
旧代码不认识新 schema，硬跑下去就是静默写坏数据，不如直接停。

**迁移前自动备份。** 只在库里确实有成交/流水时才备份，新库不产生垃圾文件。
备份用 `VACUUM INTO` 而不是拷文件：WAL 模式下直接拷主文件会漏掉还没 checkpoint
的事务，拷出来是个撕裂的库——而那恰好是你最需要它的时候。备份名形如
`perpdesk.bak-v0-20260914_175646.db`，只保留最近 5 份。

## 加一条迁移

在 `migrations.py` 的 `MIGRATIONS` 列表**末尾追加**：

```python
def _m003_accounts(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS accounts (...)")
    conn.execute("ALTER TABLE trades ADD COLUMN account_id INTEGER")
    conn.execute("UPDATE trades SET account_id = 1 WHERE account_id IS NULL")

MIGRATIONS = [
    (1, "baseline", _m001_baseline),
    (2, "trades.realized_pnl", _m002_trades_realized_pnl),
    (3, "accounts", _m003_accounts),          # ← 追加
]
```

四条规矩：

1. **永远不要改已发布的那几条。** 别人的库已经按老版本跑过了，改了等于两边 schema
   悄悄分叉，而且谁都不会发现。
2. **每条都要能在"已经是目标状态"的库上安全重跑**（`IF NOT EXISTS` / 先查后改）。
3. **不要用 `conn.executescript()`。** 它会在执行前隐式 `COMMIT` 掉当前事务，外层
   `BEGIN` 就此丢失，迁移不再是原子的：中途失败会留下半建的表，而 `user_version`
   还停在旧值，下次启动又从头跑一遍。用 `_exec_all()` 逐条执行。
   （初版基线真踩了这个坑，`test_every_migration_stays_inside_its_transaction` 就是为它留的。）
4. **数据回填写进同一条迁移**，跟 DDL 共享事务——否则表改了、数据没回填，中间态没人收拾。

## 测试

```bash
pip install -r backend/requirements-dev.txt
pytest
```

`backend/tests/test_migrations.py` 覆盖的重点不是"能不能建表"，而是出事的路径：
迁移中途失败是否整体回滚、版本倒挂是否拒绝、存量数据有没有被备份、
老库接管后数据是否逐字节不变。这些都是别人的交易历史，错一次就没了。

## 现有表

| 表 | 说明 |
|---|---|
| `watchlist` | 自选标的与排序 |
| `trades` | 成交流水。`realized_pnl` 是交易所给的每笔已实现盈亏，优先于本地回放——账户可能用过双向持仓模式，净额加权平均法在那种情况下算不对；手工录入的成交留 `NULL`，由回放补算 |
| `income` | 交易所资金流水（资金费、手续费等）。资金费不体现在成交记录里，但对长期持仓是实打实的损益，必须单独同步 |
| `_migrations` | 迁移执行日志，仅供排查 |

### 已知的待改点

`income.tran_id` 现在是**主键**。币安的 tranId 只在单账户内唯一，接入多账户或
第二家交易所时必然要改成复合键——见 ROADMAP 的层 0。
