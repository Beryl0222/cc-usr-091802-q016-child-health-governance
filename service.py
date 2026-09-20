"""儿童健康结论治理服务入口。

用法：
  python3 service.py --check            # 自检：种子目录能否通过全部治理校验
  python3 service.py                    # 启动 HTTP 服务（默认 8000 端口）
  python3 service.py --port 8080 --data data/state.json
"""

import argparse

from governance import SERVICE_ID, SERVICE_NAME
from governance.api import make_server
from governance.app import Application


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def self_check() -> None:
    """装配一次应用：规则会签、指纹、文案红线、设备目录全部通过才算健康。"""
    app = Application()
    assert health_payload()["service"] == SERVICE_ID
    rules = list(app.rulebook._rules.values())
    assert rules, "规则库为空"
    for r in rules:
        assert len(r.approvals) == 3, f"{r.rule_id} 未完成三方会签"
        assert r.fingerprint.startswith("sha256:"), f"{r.rule_id} 缺少内容指纹"
    assert "watch-k1" in app.devices
    report = app.stats.quality_report(["model_id"])
    assert report["min_cohort"] == 50
    print(f"基础检查通过：{len(rules)} 条已会签规则，{len(app.devices)} 个设备型号")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--data", default=None, help="状态持久化文件（JSON）")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        self_check()
        return
    app = Application(data_path=args.data)
    app.load()
    server = make_server(args.host, args.port, app)
    print(f"{SERVICE_NAME} 已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
