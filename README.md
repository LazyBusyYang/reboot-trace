# Reboot Trace

面向 Linux 容器异常重启调查的轻量取证系统。后端持续采集目标环境资源、各维度 Top-N 进程与用户汇总，并在有限 SQLite 空间内跨容器实例生命周期保存；Vue 前端通过 HTTPS Ingress 跨域聚合多个后端。

## 后端开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-test.lock
PYTHONPATH=backend .venv/bin/pytest
RT_PROJECT_DIR="$PWD" RT_PROC_ROOT=/proc RT_HOST_PASSWD=/etc/passwd RT_CORS_ORIGINS=http://localhost:5173 PYTHONPATH=backend .venv/bin/uvicorn reboot_trace.main:app --reload
```

后端必须作为普通进程直接运行在目标容器内，使用本地 `/proc` 和目标容器根 overlay 中的实例 marker。标准部署默认使用 `/run/reboot-trace/container-instance-id`；无法由 root 准备目录的 dev1/4/5/9 使用 `scripts/instances/`，由普通用户在 `/tmp/reboot-trace-${UID}` 创建私有 marker。数据默认建议放在 `<项目根>/var/reboot-trace`；Git 项目目录本身不要求持久，但运维人员必须在启动前确认这个实际数据路径位于适合 SQLite 且能跨目标容器重建保留的挂载，否则应显式设置 `RT_DATA_DIR`。程序会拒绝明显位于根 overlay 或与 marker 同挂载的配置，但不会代替真实重建验收。

后端使用 format v2 生命周期证据存储：`reboot-trace.sqlite3` 是 DELETE journal 模式的活动滚动库，`evidence/` 为每个已结束生命周期保存一个不可变胶囊，`evidence.json` 可由胶囊内元数据重建。每个生命周期默认只保留最后 12 个完整快照，以及最近一小时内按 5 分钟桶保留的最多 12 个 `summary_only` 趋势快照。总预算仍由 `RT_STORAGE_LIMIT_MIB` 控制；达到 85% 后按完整生命周期删除最老胶囊，直到低于 75%。运行期间不使用 WAL、incremental vacuum 或原地压缩。

滚动上限可通过 `RT_FINAL_SNAPSHOTS_PER_LIFECYCLE`、`RT_TREND_SNAPSHOTS_PER_LIFECYCLE` 和 `RT_TREND_INTERVAL_SECONDS` 调整；完整快照至少为 1，趋势数量可设为 0，趋势间隔不得小于采样间隔。

现有 schema v3 单库不会在启动时静默转换。停服并把备份目录放在 `RT_DATA_DIR` 之外后，先预检再迁移：

```bash
PYTHONPATH=backend python3 -m reboot_trace.storage_repack --data-dir "$RT_DATA_DIR" --backup-dir /path/outside-data --dry-run
PYTHONPATH=backend python3 -m reboot_trace.storage_repack --data-dir "$RT_DATA_DIR" --backup-dir /path/outside-data
```

迁移会执行完整性和外键检查、生成带 SHA-256 的备份，并在安装阶段保持独占锁。安装使用 `.evidence-repack.json` 记录可恢复阶段；中断后使用相同命令和备份根目录即可幂等续跑。旧分段只在全部新胶囊校验通过后移出生产布局，损坏、占用或空间不足时不会开始安装。旧的 `storage_migrate` 命令保留为兼容入口，但同样生成 format v2 布局。

## 前端开发

```bash
cd frontend
npm install
npm run dev
```

编辑 `frontend/public/config/runtime-config.json`，将 `baseUrl` 配置为各后端 HTTPS Ingress 的 `/api/v1` 地址。浏览器请求不携带 Cookie 或 Authorization。
当前前端兼容 schema 1–3；升级后端到 schema v3 前应先发布新前端，并继续挂载现有生产 `runtime-config.json`，不要把 Ingress 地址写入镜像。

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

## 前端镜像发布

前端镜像发布到 `dockersenseyang/reboot-trace`，仅构建 `linux/amd64`，且不会创建或移动 `latest` 标签。
仓库需要配置 GitHub Actions Secrets：`DOCKERHUB_USERNAME` 和具有该仓库推送权限的
`DOCKERHUB_TOKEN`。

- 正式版本以根目录 `VERSION` 为唯一发布版本。推送匹配的 `v${VERSION}` Git 标签，或在
  `main`、`release/**` 分支提交中修改 `VERSION`，会在前端测试、生产构建和镜像构建通过后发布
  `dockersenseyang/reboot-trace:${VERSION}`。已存在的同名远端镜像会被幂等跳过。
- Pull Request 和未修改 `VERSION` 的普通分支提交只执行校验，不登录 Docker Hub，也不推送镜像。
- `Frontend image manual (dev / sha-*)` workflow 可手动选择 Git ref，并只允许发布 `dev` 或
  `sha-*` 标签，例如 `dockersenseyang/reboot-trace:sha-abc1234`。

发布前可在本地执行 `bash scripts/check_version.sh`；正式 Git 标签必须严格等于 `v${VERSION}`。
