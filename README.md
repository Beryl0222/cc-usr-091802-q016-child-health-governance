# 儿童健康结论治理

用于管理儿童设备测量质量、健康规则版本、监护同意和安全提示。

`fixtures/sample.json` 保存可公开的领域样例，只用于说明数据边界，不包含真实个人资料或业务凭据。

执行 `python3 service.py --check` 可检查项目身份，运行 `python3 -m unittest discover -s tests -v` 可核对基础契约。服务启动后，`/health` 返回项目标识。
