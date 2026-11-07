# Tailscale Serve 私网 HTTPS 入口

本文描述生产受控试点的员工入口方案，不是已部署记录。DTC API 只发布在主机回环
`127.0.0.1:8000`，Tailscale Serve 在 tailnet 内终止 HTTPS 并代理到该地址。PostgreSQL
不发布主机端口，也不允许从 tailnet 或公网直接访问。

## 拓扑与前置条件

```text
员工设备 --Tailscale(tailnet)--> Tailscale Serve(443)
                                      |-- /staff, /staff/*   --> 127.0.0.1:8000/staff...
                                      |-- /api/*             --> 127.0.0.1:8000/api...
                                      |-- /assets, /assets/* --> 127.0.0.1:8000/assets...
                                      |-- /favicon.svg       --> 127.0.0.1:8000/favicon.svg
                                      `-- /metrics           --> 不挂载，返回 404
```

- 主机已安装并登录 Tailscale；开启 MagicDNS 和 HTTPS 证书。
- DTC 主机使用独立 tag（示例 `tag:dtc-host`），员工设备使用独立用户组（示例
  `group:dtc-staff`）。不要用个人管理员设备代替员工组。
- `docker-compose.production.yml` 的 API 端口映射必须是
  `127.0.0.1:8000:8000`；不得改成 `0.0.0.0:8000` 或使用 `network_mode: host`。
- 生产 env 文件必须通过 `DTC_ENV_FILE` 显式提供，且权限为 `0400` 或 `0600`。

以下命令中的 `<host>.<tailnet>.ts.net` 是占位 MagicDNS 名称。若使用受控自有域名，
先完成 DNS 与 `tailscale cert` 验证，再替换命令中的名称；不要在文档或仓库写入真实
tailnet 名称、token 或员工身份。

## ACL

在 tailnet 管理控制台使用最小权限策略。下面只是结构示例，必须替换占位用户并在
实际 tailnet 做一次拒绝测试：

```json
{
  "groups": {
    "group:dtc-staff": ["staff-a@example.invalid", "staff-b@example.invalid"]
  },
  "tagOwners": {
    "tag:dtc-host": ["autogroup:admin"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["group:dtc-staff"],
      "dst": ["tag:dtc-host:443"]
    }
  ]
}
```

要求：

- 只允许员工组访问 `tag:dtc-host:443`；不要放行 `22`、`8000`、`5432` 或任意端口。
- Tailscale ACL 默认拒绝未匹配流量；不要添加 `*:*` 兜底规则。
- 不启用 Tailscale SSH，除非另有独立审计的主机管理方案。
- 移除员工时同时从组和 DTC 员工凭证中停用；两者缺一都不能视为已停用。

## 启用入口

先确认没有根路径挂载。Tailscale Serve 会剥离挂载前缀，因此代理目标要带上同样的
前缀，才能让 FastAPI 收到原始 `/staff/...` 和 `/api/...` 路径：

```bash
sudo tailscale serve status
sudo tailscale serve --bg --https=443 --set-path=/staff http://127.0.0.1:8000/staff
sudo tailscale serve --bg --https=443 --set-path=/api http://127.0.0.1:8000/api
sudo tailscale serve --bg --https=443 --set-path=/assets http://127.0.0.1:8000/assets
sudo tailscale serve --bg --https=443 --set-path=/favicon.svg http://127.0.0.1:8000/favicon.svg
sudo tailscale serve status
```

允许的路径：

| 路径 | 用途 | 是否经 Tailscale 入口 |
|---|---|---|
| `/staff`、`/staff/*` | 员工工作台 | 是 |
| `/api/*` | DTC API | 是 |
| `/api/v1/health` | 存活检查 | 是 |
| `/api/v1/ready` | 就绪检查 | 是 |
| `/assets`、`/assets/*` | 前端静态资源（经审查的构建产物） | 是 |
| `/favicon.svg` | 站点图标 | 是 |
| `/metrics` | Prometheus 指标 | **否**，只允许主机本机访问 |
| `/` 及其他路径 | 无生产用途 | 否 |

不要在 `443` 上挂载 `/`。根挂载会把 `/metrics`、env 文件和其他所有未审查路径一起
暴露，并可能与子路径冲突。`/assets` 和 `/favicon.svg` 是仅有的两个静态资源挂载，必须
保持精确路径；不要扩大为其他目录或添加其他静态路径。不要为了调试临时添加 `/metrics`
或根路径。

## 验证

在员工设备上：

```bash
curl -fsS "https://<host>.<tailnet>.ts.net/api/v1/health"
curl -fsS "https://<host>.<tailnet>.ts.net/api/v1/ready"
curl -fsS -o /dev/null -w '%{http_code}\n' "https://<host>.<tailnet>.ts.net/staff"
# 静态资源必须从精确挂载返回 200
curl -fsS -o /dev/null -w '%{http_code}\n' "https://<host>.<tailnet>.ts.net/favicon.svg"
asset_path=$(
  curl -fsS "https://<host>.<tailnet>.ts.net/staff" |
    grep -oE '/assets/[^" ]+' | head -n 1
)
curl -fsS -o /dev/null -w '%{http_code}\n' "https://<host>.<tailnet>.ts.net${asset_path}"
# 期望 404，不能返回指标正文或根路径内容
curl -sS -o /dev/null -w '%{http_code}\n' "https://<host>.<tailnet>.ts.net/metrics"
curl -sS -o /dev/null -w '%{http_code}\n' "https://<host>.<tailnet>.ts.net/"
```

在主机上确认监听范围：

```bash
ss -ltnp | grep -E ':(8000|5432)\b'
# 8000 只应出现在 127.0.0.1；5432 不应出现在主机监听列表。
docker compose --env-file /dev/null -f docker-compose.production.yml ps
```

从公网或未授权设备验证不可达：

```bash
curl --connect-timeout 5 -fsS "http://<public-ip>:8000/api/v1/health" && echo UNEXPECTED
nc -vz -w 3 "<public-ip>" 5432
```

两条命令都必须失败。云安全列表/防火墙不应对公网开放 `8000` 或 `5432`；主机防火墙
也应默认拒绝入站。Tailscale ACL 的拒绝测试应使用不在 `group:dtc-staff` 的账号完成，
不能只验证允许路径。

## 临时阻断入口

按影响范围从小到大执行，并记录实际命令与时间：

1. 移除 Serve 路径（保留 tailnet 连接，便于管理）：

   ```bash
   sudo tailscale serve reset
   sudo tailscale serve status
   ```

2. 若需要阻断整个 tailnet 访问（包括其他管理流量），再执行：

   ```bash
   sudo tailscale down
   tailscale status
   ```

3. 在 ACL 中临时移除 `group:dtc-staff` 到 `tag:dtc-host:443` 的允许规则；该变更应
   经审批并在事件结束后恢复。

4. 如需停止业务进程，先停 Worker 再停 API，避免 Worker 继续领取新操作：

   ```bash
   docker compose --env-file /dev/null -f docker-compose.production.yml stop worker
   docker compose --env-file /dev/null -f docker-compose.production.yml stop api
   ```

恢复时按原顺序重新执行上述四条 `tailscale serve --set-path` 命令，并在员工设备上重跑
健康检查。`tailscale serve reset` 会清空所有 Serve 规则，不只是本服务；执行前确认
主机没有其他共享入口。紧急停用的业务侧顺序和凭证轮换要求见
`docs/11-deployment.md` 的“失败与回滚”章节。

## 不在本文件范围内

- 未申请域名、未创建 Tailscale ACL、未部署任何服务，也未验证真实员工身份。
- 未配置备份、告警、SSO 或高可用；单机故障域和 Tailscale 依赖仍属于试点风险。
- 公网无法访问的结论必须在实际云主机上保留命令输出，不能由本方案推断。
