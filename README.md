# Reboot Trace

面向 Linux 跳板机异常重启调查的轻量取证系统。后端持续采集宿主机资源、各维度 Top-N 进程与用户汇总，并在有限 SQLite 空间内跨 boot 生命周期保存；Vue 前端通过 HTTPS Ingress 跨域聚合多个后端。

## 后端开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-test.lock
PYTHONPATH=backend .venv/bin/pytest
RT_DATA_DIR=/tmp/reboot-trace RT_PROC_ROOT=/proc RT_HOST_PASSWD=/etc/passwd RT_CORS_ORIGINS=http://localhost:5173 PYTHONPATH=backend .venv/bin/uvicorn reboot_trace.main:app --reload
```

容器部署必须提供宿主机 PID namespace，并只读挂载 `/proc` 和可选 `/etc/passwd`，详见 `docs/deployment.html`。

## 前端开发

```bash
cd frontend
npm install
npm run dev
```

编辑 `frontend/public/config/runtime-config.json`，将 `baseUrl` 配置为各后端 HTTPS Ingress 的 `/api/v1` 地址。浏览器请求不携带 Cookie 或 Authorization。

## 验收

```bash
PYTHONPATH=backend pytest -q
python3 docs/sync_styles.py --check
cd frontend && npm run build
```

普通验收入口为 `bash scripts/acceptance.sh --skip-docker`。具备 Docker CLI 的发布环境应运行
`bash scripts/acceptance.sh` 完成镜像构建门禁；跳过 Docker 的结果只代表源码、测试、文档和前端构建通过，
不得表述为镜像已经验收。

镜像文件位于 `backend/Dockerfile` 和 `frontend/Dockerfile`；Compose 示例位于 `deploy/compose.example.yaml`。
