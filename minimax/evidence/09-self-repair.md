# 09 · 自修复通道验证（故障注入）

本文件是对 `08-summary.json` 中 `repair_rounds=0`（一次通过）的补充验证：
人为注入缺陷后，把 pytest 失败输出回喂 MiniMax，验证 pipeline 的「失败自修复」通道真实可用。

## 注入的缺陷（两处，模拟常见疏漏）

1. `reserve()` 中漏掉 `self._check_ttl(ttl_seconds)`，非法 TTL 不再抛 `InventoryError`；
2. `_expire_due_reservations()` 的判定从 `expires_at <= now` 改成 `expires_at < now - 60`，过期回收被延后 60 秒。

## 第 1 轮：缺陷状态下跑测试

```
exit=1
FAILED tests/test_ttl.py::ReservationTTLTests::test_commit_after_expiration_returns_false
FAILED tests/test_ttl.py::ReservationTTLTests::test_expired_reservation_restores_available_stock
FAILED tests/test_ttl.py::ReservationTTLTests::test_non_integer_ttl_raises_inventory_error
FAILED tests/test_ttl.py::ReservationTTLTests::test_non_positive_ttl_raises_inventory_error
FAILED tests/test_ttl.py::ReservationTTLTests::test_release_after_expiration_returns_false
5 failed, 14 passed
```

## 第 2 轮：MiniMax 修复

- 输入：`specs/spec-v2.md` + 当前 `service.py` + 上一步 pytest 输出
- 模型：`MiniMax-M3.1-Flash`
- 耗时：45.17s
- usage：`{"total_tokens": 10007, "total_characters": 0, "prompt_tokens": 2788, "completion_tokens": 7219, "completion_tokens_details": {"reasoning_tokens": 6196}, "prompt_tokens_details": {"cached_tokens": 187}}`
- 输出：一轮即修复，无需第 2 轮

```
exit=0
19 passed in 0.10s
```

## 结论

| 指标 | 结果 |
|---|---|
| 修复轮次 | 1 |
| 修复后全量用例 | 19 passed（12 回归 + 7 功能） |
| 回归是否被破坏 | 否（12 个既有用例断言未改动） |
| 调用记录 | [`09-repair-calls.jsonl`](./09-repair-calls.jsonl) |

## 环境

Windows / Python 3.9.13 / pytest 8.4.2（业务代码无第三方依赖，跨平台一致）。

## MiniMax 修复版与参考实现的差异

```diff
--- a/src/inventory/service.py (reference)
+++ b/src/inventory/service.py (MiniMax repaired)
@@ -59,11 +59,14 @@
         self._check_ttl(ttl_seconds)
         if sku not in self._stock:
             raise InventoryError("未知 sku: %s" % sku)
-        if quantity > self.get_available(sku):
+
+        available = self.get_available(sku)
+        if quantity > available:
             raise InventoryError(
                 "可用库存不足: sku=%s 需要=%d 可用=%d"
-                % (sku, quantity, self.get_available(sku))
+                % (sku, quantity, available)
             )
+
         reservation = Reservation(
             reservation_id="rsv-" + uuid.uuid4().hex[:12],
             sku=sku,
```
