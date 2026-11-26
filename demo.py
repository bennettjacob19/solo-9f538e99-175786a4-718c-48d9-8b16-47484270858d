"""端到端演练脚本：升档成功 -> 迟到的监控更正 -> 触发回退 -> 试请求只走稳定版。

用法：python demo.py
（使用独立临时 SQLite 库与 Flask 测试客户端，不影响正式 data/gray.db）
"""
import os
import tempfile

os.environ.setdefault("GRAY_DB_PATH",
                      os.path.join(tempfile.mkdtemp(prefix="gray-demo-"), "demo.db"))
os.environ.setdefault("GRAY_STABLE_PORT", "18281")
os.environ.setdefault("GRAY_CANDIDATE_PORT", "18282")

from app import create_app  # noqa: E402

app = create_app()
client = app.test_client()


def show(title, resp):
    text = resp.get_data(as_text=True)
    import re
    flashes = re.findall(r'<div class="flash flash-[^"]+">([^<]+)</div>', text)
    print(f"\n===== {title} =====")
    for msg in flashes:
        print("提示：", msg)
    if "已升档" in text:
        print("页面状态：已升档（候选版参与分流）")
    if "原档 / 已回退" in text:
        print("页面状态：原档 / 已回退（新请求只走稳定版）")


def post(path, data):
    return client.post(path, data=data, follow_redirects=True)


# 1) 补报 3 个 UTC 分钟：300 请求、3 失败（错误率 1%）
minutes = ["2026-11-26T09:00", "2026-11-26T09:01", "2026-11-26T09:02"]
r = post("/correct", {
    "minute[]": minutes,
    "requests[]": ["100", "100", "100"],
    "failures[]": ["1", "1", "1"],
})
show("步骤1 补报 3 分钟（300 请求 / 3 失败，1%）", r)

# 2) 升档：比例 20%，错误率上限 5%
r = post("/ramp", {
    "window_start": "2026-11-26T09:00",
    "window_end": "2026-11-26T09:02",
    "target_ratio": "20.00",
    "error_limit": "5.00",
})
show("步骤2 升档（20% 分流，上限 5%）", r)

# 3) 升档期间试请求（真实本地应答服务）
for i in range(3):
    show(f"步骤3.{i} 升档期间试请求", post("/trial", {}))

# 4) 迟到更正：09:00 失败数从 1 改成 40 -> 合计 300/42 = 14% > 5%
r = post("/correct", {
    "minute[]": ["2026-11-26T09:00"],
    "requests[]": ["100"],
    "failures[]": ["40"],
})
show("步骤4 迟到更正（09:00 失败 1 -> 40，整段 14% > 5%）", r)

# 5) 回退后试请求：应全部命中稳定版
for i in range(3):
    show(f"步骤5.{i} 回退后试请求（应只走稳定版）", post("/trial", {}))

# 6) 重新升档被拒演示：数据仍超限，维持原档
r = post("/ramp", {
    "window_start": "2026-11-26T09:00",
    "window_end": "2026-11-26T09:02",
    "target_ratio": "20.00",
    "error_limit": "5.00",
})
show("步骤6 数据未修复时重新升档（应拒绝并提示超限）", r)

print("\n演练结束。")
