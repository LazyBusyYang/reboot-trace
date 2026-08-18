# 开发机 1、4、5、9 后端运行说明

四个容器共享项目代码和虚拟环境，但分别使用 `runtime/dev-{1,4,5,9}` 下的数据、日志和 PID 目录。默认监听端口为 `31088`，允许的前端 Origin 为 `https://maoshanwang-reboot-trace.zoedev.top`。

由于后端只能以普通用户运行，实例 marker 使用 `/tmp/reboot-trace-${UID}/container-instance-id`。公共脚本创建权限为 `0700` 的私有目录，并要求 PID 1 将该路径识别为容器根 overlay。不要把这个目录映射为 volume、hostPath、PVC、emptyDir 或独立 tmpfs。

在对应开发机执行：

```bash
scripts/instances/start-dev-4.sh start
scripts/instances/start-dev-4.sh status
scripts/instances/start-dev-4.sh restart
scripts/instances/start-dev-4.sh stop
```

`run` 会以前台方式运行，适合交给外部进程管理器。脚本固定使用：

```text
/mnt/aigc/gaoyang3/Code/github/tunnel-preview/.venv/bin/python3
/mnt/aigc/gaoyang3/Code/github/tunnel-preview/.venv/bin/uvicorn
```

启动前会验证 namespace、marker、持久挂载、实例绑定和迁移余量；启动后会验证 schema v3、身份能力和数据路径。脚本把 PID、Linux start ticks、数据目录、marker、端口和随机 service token 原子写入权限 0600 的进程状态文件；HTTP 状态响应必须回显同一 token。停止前会再次核对完整进程身份，验证失败时保留状态文件且不发送 SIGTERM。任何检查失败都会拒绝上线。

旧版本只包含数字 PID 的文件会被识别为 legacy。`stop` 仅在 cmdline、端口和 `RT_DATA_DIR` 全部匹配当前实例后才停止旧进程；`start` 不会覆盖仍在运行或身份存疑的 legacy PID 文件。

放弃某个实例的旧历史时，应先停止服务并把完整 data 目录备份到该目录之外；活动库、`segments/`、`segments.json` 和 `quarantine/` 必须作为同一存储集合处理，保留 `host-id` 与 `bound-hostname`。不要在后端运行时删除或复制数据库文件。旧 schema v3 单库必须使用 `python -m reboot_trace.storage_migrate` 显式离线迁移，不能依赖启动脚本自动转换。
