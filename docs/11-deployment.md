# 11 · 部署与运维

> 既有默认Compose仍使用单API与SQLite。本批增加独立售后Compose候选：PostgreSQL、显式迁移、API和Worker；验收状态见[流程记录](records/after-sales/dtc-workflow.md)。它默认Fake，仅验证部署基础，不代表两仓共享业务系统已联调。

## 本章目录

1. [本地](#本地)
2. [Docker Compose](#docker-compose)
3. [生产检查清单](#生产检查清单)
4. [扩容、知识库、回滚](#扩容知识库回滚)

## 本地

SQLite + 内存向量，见 [02](02-getting-started.md)。

## Docker Compose

```bash
docker compose up --build -d
```

启动后访问 `http://127.0.0.1:8000`，停止服务使用 `docker compose down`。数据保存在 Compose volume `adaptive-rag-data` 中。

### 三种运行模式

| 模式 | Compose 文件 | 组件 | 适用场景 |
|---|---|---|---|
| 默认演示 | `docker-compose.yml` | API、前端、SQLite | 最少依赖，自动降级 |
| 单机生产 | `docker-compose.single.yml` | API、前端、PostgreSQL、Redis | 单节点长期运行 |
| 多实例生产 | `docker-compose.multi.yml` | Nginx、双 API、PostgreSQL、Redis、Qdrant | 扩容和共享状态 |

```bash
# 单机和多实例模式先设置数据库密码
export POSTGRES_PASSWORD='change-this-password'
docker compose -f docker-compose.single.yml up --build -d
docker compose -f docker-compose.multi.yml up --build -d
```

单机和多实例镜像会安装 `.[rerank]`。启用本地 Embedding / Cross-Encoder 时，首次启动可能下载模型；没有 LLM 密钥时仍会使用离线证据回答。

| 容器 | 作用 | API 依赖 |
|---|---|---|
| `api` | FastAPI | 8000 |

### 代码摘要

```yaml
# docker-compose.yml
api:
  environment:
    DATABASE_URL: sqlite+aiosqlite:///./data/local/app.db
    REDIS_URL: ""
    QDRANT_URL: ""
    APP_ENV: production
```

健康检查打 `http://127.0.0.1:8000/api/v1/health`。镜像非root，按`uv.lock`执行frozen依赖安装，应用通过`/app`的PYTHONPATH运行，包含显式Alembic迁移。

## 生产检查清单

- [ ] `APP_ENV=production`，`LOG_LEVEL=INFO`
- [ ] `ADMIN_API_TOKEN` 已设
- [ ] `DATABASE_URL` 指向 Postgres，连接池已看过
- [ ] 真实 embedding 网关（否则对外以 BM25 为准）
- [ ] `pip install -e ".[rerank]"` 后重跑 retrieval，报告含 `hybrid+cross-encoder`
- [ ] `pip install -e ".[docs]"` 后用真实 PDF/扫描件验证 ingest；换切分后重跑 `--task retrieval`
- [x] `u_demo` / `anonymous` 全局读单已关闭
- [ ] 售后启用员工Bearer并核对角色/市场配置；旧演示模式不能代替生产认证，不可直接对公网开放
- [ ] liveness / readiness 分开（见 [08](08-reliability.md#探活)）
- [ ] 密钥不在日志、镜像、README
- [ ] 生产目标按显式Alembic执行升级并演练恢复；售后PG入口已提供迁移gate，旧SQLite演示的create_all不适用于PG

## 扩容、知识库、回滚

API 无粘滞：session 在 DB。进程内限流多副本不共享 → 生产配 Redis。Graph 每进程编译一次。

改 `knowledge/` 文档后重启 API。`chunk_id` 在同一切分结果下稳定，Qdrant upsert 覆盖。无 CMS。生产 ingest 走 Unstructured；系统包需要 poppler、tesseract（`chi_sim`）、处理 Office 时的 libreoffice。换解析器后重跑 `--task retrieval`。

应用无状态（除 DB）。回滚镜像即可。Alembic `0002` 退货表，schema 回滚需显式 `downgrade`。

---

上一章 [10](10-evaluation.md) · 下一章 [12 · 限制](12-limitations.md)


## 售后隔离部署候选

`docker-compose.after-sales.yml`为本地合成环境入口，不替换旧Compose；API仅绑定127.0.0.1，PG不发布主机端口。migrate先等PG健康，API/Worker再等迁移成功。镜像保留production及禁用本地模型的安全默认，本地Compose显式覆盖development/Fake/离线；bridge允许出站，不能宣称网络层禁止外联。空admin token在development下允许本地管理信息访问，员工业务接口仍要求真实员工身份。

首次操作须选全新且明确归属的项目名，准备外部生成的合成PG密码，并使用`--env-file /dev/null`或本次0600专用文件，避免Compose自动读取仓库.env。不要在已有业务库运行本演示。镜像依uv.lock frozen安装并包含Alembic配置/0005；`NODE_IMAGE`/`PYTHON_IMAGE`构建参数可指定相同官方版本的可达镜像源，不改变Python依赖。IMAGE_REVISION只表示构建基线，有工作区叠加时还需记录候选文件digest和实际image ID。

API与Worker各自Fake不能共享外部工单，因此健康/迁移/Worker单次成功只证明部署基础。真实两仓HTTP、员工初始化、备份恢复另验；当前员工SQLite seed不适用于PG，不能把它用于已有PG库。基础启动与清理示例见下文，真实业务初始化与两仓恢复仍另验。

以下操作仅启动新的本地部署基础环境。在同一终端保留变量用于清理；API自动分配loopback端口，避免占用已有服务。凭据文件不输出到终端、不进入构建上下文：

```bash
DTC_DEMO_PROJECT="dtc-demo-$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
DTC_DEMO_ENV="$(mktemp -t dtc-after-sales)"
chmod 600 "$DTC_DEMO_ENV"
python3 - "$DTC_DEMO_ENV" "$DTC_DEMO_PROJECT" <<'PY'
import secrets, sys
from pathlib import Path
Path(sys.argv[1]).write_text(
    'POSTGRES_PASSWORD=' + secrets.token_hex(24) + '\n'
    + 'POSTGRES_USER=dtc\nPOSTGRES_DB=dtc_after_sales\nAPI_PORT=0\n'
    + 'APP_IMAGE=' + sys.argv[2] + ':local\n'
)
PY

docker compose --env-file "$DTC_DEMO_ENV" -f docker-compose.after-sales.yml \
  -p "$DTC_DEMO_PROJECT" build
# 若Docker Hub不可达，可在上述build后追加已实测的相同官方版本构建参数：
# --build-arg NODE_IMAGE=public.ecr.aws/docker/library/node:24-alpine \
# --build-arg PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.11-slim

docker compose --env-file "$DTC_DEMO_ENV" -f docker-compose.after-sales.yml \
  -p "$DTC_DEMO_PROJECT" up -d --no-build

docker compose --env-file "$DTC_DEMO_ENV" -f docker-compose.after-sales.yml \
  -p "$DTC_DEMO_PROJECT" ps -a

docker compose --env-file "$DTC_DEMO_ENV" -f docker-compose.after-sales.yml \
  -p "$DTC_DEMO_PROJECT" port api 8000
```

按输出的`127.0.0.1:端口`访问`/api/v1/health`、`/api/v1/ready`和`/staff`。预期migrate退出0、API健康、Worker运行，数据库版本0005；员工业务无token仍401。此步骤只提供登录shell，尚未初始化PG员工或接真实E-pig，不能据此宣布工作台业务演示完成。不要运行输出全部展开环境变量的`compose config`或全量容器inspect。

只清理本次随机项目，`-v`会删除该项目的合成PG数据，不要替换成既有项目名：

```bash
docker compose --env-file "$DTC_DEMO_ENV" -f docker-compose.after-sales.yml \
  -p "$DTC_DEMO_PROJECT" down -v --remove-orphans
docker image rm "$DTC_DEMO_PROJECT:local"
rm "$DTC_DEMO_ENV"
```

## 两仓隔离启动

需要验证DTC确实连接E-pig时，使用`docker-compose.after-sales-demo.yml`。准备器只归档两个明确提交，排除工作区与凭据；运行目录0700，专用配置0600，配置在构建上下文之外。E-pig Dockerfile受控副本仅补工作区依赖复制和官方基础镜像参数，摘要写入manifest，原仓库不修改。

在仓库根目录执行，SHA必须替换为计划演示的完整提交：

```bash
uv run python scripts/prepare_after_sales_demo.py prepare \
  --dtc-ref <DTC完整40位提交> \
  --epig-repo <E-pig本地仓库路径> \
  --epig-ref <E-pig完整40位提交>
```

按输出的专用`--env-file`、冻结Compose路径和随机项目名运行`up -d --build`，不要另用仓库.env。准备器支持`--dtc-node-image`、`--dtc-python-image`、`--epig-node-image`指定可达的同版本官方镜像。manifest包含版本、摘要、端口及清理归属，不要全文输出或上传。端口预选后仍可能被占用；失败时保留输出，清理本次项目并重新准备，不接管既有服务。

拓扑包含两个独立PG库、各自迁移、E-pig、DTC API与单次Worker。DTC共享E-pig网络命名空间，使既有Connector的开发环境loopback地址确实指向E-pig；不放宽生产HTTPS约束。仅E-pig容器发布两个127.0.0.1应用端口，数据库不发布端口。普通bridge仍允许出站，离线由明确配置保证。Worker此入口运行一次后应退出0；持续处理另需明确运行方式。

预期两个迁移退出0、两个PG健康、DTC ready200、无身份的员工业务401、Worker退出0。空库只验证启动，不含消费者订单、员工身份或完整建单；不要把空feed成功称为业务演示完成。完整业务及恢复证据见[流程记录](records/after-sales/dtc-workflow.md)。

清理只接受该次manifest：

```bash
uv run python scripts/prepare_after_sales_demo.py cleanup --manifest <输出的manifest路径>
```

它校验冻结Compose摘要和资源归属标签，确认所属容器、卷、网络消失后才删除配置和目录。遇到超时、标签不符或资源残留会保留配置供排查；不使用全局prune。镜像保留，可按manifest中本次镜像标签单独删除，不删除共享基础镜像。

## 售后数据库恢复的操作顺序

备份不能撤销已在E-pig提交的工单。DTC数据库恢复后，应先查询原幂等键的外部回执，避免重新创建。已验证的窗口是外部提交成功、DTC尚未收到回执；其他故障窗口需要单独验证。

1. 停止Worker并暂停DTC写入；确认数据库目标和备份权限。使用`pg_dump --format=custom`输出0600备份，禁止把连接密码写入命令历史或日志。
2. 在另一个明确归属的全新数据库恢复，先核对迁移版本0005、草稿、批准、游标及操作原键/payload；不要覆盖来源不明或正在使用的库，也不要同步回滚E-pig。
3. 保持Worker停止，连接恢复库启动API。对未完成操作等待租约自然到期，由授权操作员使用现有回执查询/对账入口核对原票据；回执关联不符或查不到时保留人工处理，不能伪造完成或换新键重发。
4. 确认原批准与草稿按核验结果消费、外部票据仍唯一，再恢复Worker；核对无重复外部POST。备份、恢复目标和原始日志按保留策略管理。

上述顺序在专用合成环境实际验证过，不提供生产RPO/RTO保证。现有临时验收工具没有作为通用备份平台交付；生产备份调度、异地存储、加密和恢复演练需要部署环境另行确定。

## 独立浏览器演示与验收

这条入口用合成订单和员工，在真实页面完成一次闭环，保留截图/报告后清理测试环境；不需要ChatGPT扩展或用户Chrome。它固定已验收的两个源码提交，不自动跟随分支HEAD，不是长期生产部署入口。前提是本仓Python开发依赖、Node24、前端锁定依赖，以及E-pig的Node/pnpm依赖已安装；Docker可用，固定提交在本地可读取。

首次使用由环境管理者建立本项目专用缓存，不要把用户Chrome目录当缓存或给其他目录补归属标记：

```bash
npm ci --prefix frontend
DTC_QA_ENGINE="$HOME/.cache/dtc-after-sales-browser/engine-playwright-1.65.0"
mkdir -p "$DTC_QA_ENGINE"
chmod 700 "$DTC_QA_ENGINE"
PLAYWRIGHT_BROWSERS_PATH="$DTC_QA_ENGINE" \
  node frontend/node_modules/playwright/cli.js install chromium
python3 - "$DTC_QA_ENGINE" <<'PY'
import os, sys
from pathlib import Path
marker = Path(sys.argv[1]) / '.dtc-owned'
marker.write_text('DTC dedicated synthetic browser acceptance cache\n')
os.chmod(marker, 0o600)
PY
```

浏览器下载失败应先排查环境，不关闭TLS校验、不回退用户浏览器。缓存可复用；若设置`DTC_BROWSER_RUN_ROOT`，安装位置也必须对应到该专用根下。

```bash
uv run python scripts/run_after_sales_browser.py --epig-repo /path/to/E-pig
```

预期四组分别验证：消费者申请到人工批准后的唯一工单回复；第二消费者隔离与未知提交原键恢复；员工冲突/角色/退出/AI不可用的手工处理；桌面和320px、关键键盘与错误/加载状态。动作通过DOM完成，之后用所属数据库独立核对唯一计数。正常故事创建的是经人工批准的`other`后续处理工单，不宣称通过退货资格或执行退款。

运行输出报告/截图路径和结果；配置与凭据保存在0700/0600目录并在结束删除。Node与独立Chromium分别记录精确进程归属，超时/取消也检查整组；清理失败保留失败状态，不因父进程退出宣称浏览器关闭。报告与截图仅含合成数据，仍应按本机证据保留策略管理。截图须人工检查，自动测试不是完整可访问性认证。

普通Python单测不需要Node或浏览器。真实浏览器在Node被杀后的清理故障验收必须单独启用；显式运行而缺引擎会失败，默认跳过不算通过：

```bash
DTC_RUN_BROWSER_FAULT_ACCEPTANCE=1 uv run pytest -q \
  tests/unit/test_after_sales_browser_runner.py::test_actual_detached_browser_group_is_cleaned_after_node_kill
```
