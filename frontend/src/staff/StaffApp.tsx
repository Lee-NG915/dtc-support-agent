import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { KeyRound, LogOut, ShieldCheck } from "lucide-react";
import {
  StaffApi,
  StaffApiError,
  demoLogin as requestDemoLogin,
  getLoginOptions,
  isSessionEndingError,
  loginStaff,
} from "./api";
import { StaffContext } from "./context";
import type { StaffContextValue } from "./context";
import {
  MARKET_LABELS,
  ROLE_LABELS,
  ROLE_ORDER,
  errorMessage,
  formatDateTime,
  nowIso,
} from "./format";
import { RequestGuard } from "./guard";
import type { RunResult } from "./guard";
import { EmptyBlock, Notice } from "./StaffBits";
import { OperatorPanel } from "./OperatorPanel";
import { StaffWorkbench } from "./StaffWorkbench";
import type { LoginOptionsResponse, Market, StaffIdentity } from "./types";
import "./staff.css";

type WorkspaceMode = "business" | "operations";

type LoginRequest =
  | { kind: "token"; token: string }
  | { kind: "demo"; staffId: string; password: string };

function hasBusinessRole(roles: readonly string[]): boolean {
  return roles.some((role) => role === "support" || role === "approver" || role === "auditor");
}

function defaultMode(identity: StaffIdentity): WorkspaceMode {
  return hasBusinessRole(identity.roles) ? "business" : "operations";
}

export default function StaffApp() {
  const guardRef = useRef<RequestGuard | null>(null);
  if (guardRef.current === null) guardRef.current = new RequestGuard();
  const guard = guardRef.current;
  const loginControllerRef = useRef<AbortController | null>(null);

  const [api, setApi] = useState<StaffApi | null>(null);
  const [identity, setIdentity] = useState<StaffIdentity | null>(null);
  const [market, setMarket] = useState<Market | null>(null);
  const [mode, setMode] = useState<WorkspaceMode>("business");
  const [generation, setGeneration] = useState(guard.generation);
  const [lastReadAt, setLastReadAt] = useState<string | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [loginNotice, setLoginNotice] = useState<string | null>(null);
  const [loginOptions, setLoginOptions] = useState<LoginOptionsResponse | null>(null);
  const [loginOptionsNotice, setLoginOptionsNotice] = useState<string | null>(null);
  const [demoStaffId, setDemoStaffId] = useState("");

  // Login-mode discovery is public. A failed request falls back to the
  // existing bearer form instead of blocking the entry point.
  useEffect(() => {
    const controller = new AbortController();
    getLoginOptions(controller.signal)
      .then((options) => {
        if (controller.signal.aborted) return;
        setLoginOptions(options);
        setLoginOptionsNotice(null);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        setLoginOptions({ mode: "staff_token", accounts: [] });
        setLoginOptionsNotice("无法读取演示登录配置，仍可使用员工凭证登录。");
      });
    return () => controller.abort();
  }, []);

  // A late login response after unmount must not restore an identity.
  useEffect(() => {
    return () => {
      loginControllerRef.current?.abort();
      guard.bump();
    };
  }, [guard]);

  const demoAccounts = useMemo(
    () => (loginOptions?.mode === "demo_password" ? loginOptions.accounts : []),
    [loginOptions],
  );
  const demoMode = demoAccounts.length > 0;
  const defaultDemoStaffId = useMemo(() => {
    const support = demoAccounts.find((account) => account.roles.includes("support"));
    return support?.staff_id ?? demoAccounts[0]?.staff_id ?? "";
  }, [demoAccounts]);
  const selectedDemoStaffId = demoAccounts.some(
    (account) => account.staff_id === demoStaffId,
  )
    ? demoStaffId
    : defaultDemoStaffId;

  useEffect(() => {
    setDemoStaffId((current) => {
      if (demoAccounts.some((account) => account.staff_id === current)) return current;
      return defaultDemoStaffId;
    });
  }, [demoAccounts, defaultDemoStaffId]);

  const invalidateSession = useCallback((atGeneration: number, reason?: unknown) => {
    if (!guard.isCurrent(atGeneration)) return;
    guard.bump();
    setGeneration(guard.generation);
    setApi(null);
    setIdentity(null);
    setMarket(null);
    setLastReadAt(null);
    setLoginBusy(false);
    setLoginError(
      reason && isSessionEndingError(reason)
        ? "身份或权限已失效，所有旧请求与页面数据已清除，请重新登录。"
        : "登录会话已结束，请重新登录。",
    );
    setLoginNotice(null);
  }, [guard]);

  // A context value belongs to exactly one API + generation. Old closures keep
  // their own pair, so a delayed retry from a previous market/login can only
  // observe `stale` — it can never send with the new bearer or sign the new
  // identity out with a late 403.
  const run = useCallback(
    async <T,>(
      work: (client: StaffApi, signal: AbortSignal) => Promise<T>,
    ): Promise<RunResult<T>> => {
      if (api === null) return { ok: false, stale: true };
      return guard.runWithAuth(generation, (signal) => work(api, signal), invalidateSession);
    },
    [api, generation, guard, invalidateSession],
  );

  const reportSessionEnding = useCallback(
    (error: unknown, atGeneration: number) => {
      if (guard.isCurrent(atGeneration) && isSessionEndingError(error)) {
        invalidateSession(atGeneration, error);
      }
    },
    [guard, invalidateSession],
  );

  const markRead = useCallback((value?: string) => {
    setLastReadAt(value ?? nowIso());
  }, []);

  async function login(request: LoginRequest) {
    if (loginBusy) return;
    if (request.kind === "token" && !request.token.trim()) return;
    if (request.kind === "demo" && (!request.staffId || !request.password)) return;
    loginControllerRef.current?.abort();
    const controller = new AbortController();
    loginControllerRef.current = controller;
    guard.bump();
    const atGeneration = guard.generation;
    setGeneration(atGeneration);
    setApi(null);
    setLoginBusy(true);
    setLoginError(null);
    setLoginNotice(null);
    try {
      let token: string;
      let nextIdentity: StaffIdentity;
      if (request.kind === "demo") {
        const session = await requestDemoLogin(
          request.staffId,
          request.password,
          controller.signal,
        );
        token = session.token;
        nextIdentity = session.identity;
      } else {
        token = request.token.trim();
        nextIdentity = await loginStaff(token, controller.signal);
      }
      if (!guard.isCurrent(atGeneration) || controller.signal.aborted) return;
      if (!Array.isArray(nextIdentity.roles) || !Array.isArray(nextIdentity.allowed_markets)) {
        throw new Error("invalid identity");
      }
      if (nextIdentity.allowed_markets.length === 0) {
        setLoginError("当前身份没有可访问的市场权限，请联系管理员。");
        return;
      }
      setApi(new StaffApi(token));
      setIdentity(nextIdentity);
      setMarket(nextIdentity.allowed_markets[0] ?? null);
      setMode(defaultMode(nextIdentity));
      setLastReadAt(null);
    } catch (error) {
      if (!guard.isCurrent(atGeneration) || controller.signal.aborted) return;
      if (error instanceof Error && error.message === "invalid identity") {
        setLoginError("身份响应格式不正确，请重新登录。");
      } else if (
        request.kind === "demo" &&
        error instanceof StaffApiError &&
        error.status >= 400 &&
        error.status < 500
      ) {
        // Keep account/password failures indistinguishable to the user.
        setLoginError("登录失败，请确认身份和密码后重试。");
      } else {
        setLoginError(errorMessage(error));
      }
    } finally {
      if (guard.isCurrent(atGeneration)) setLoginBusy(false);
      if (loginControllerRef.current === controller) loginControllerRef.current = null;
    }
  }

  function logout() {
    loginControllerRef.current?.abort();
    loginControllerRef.current = null;
    guard.bump();
    setGeneration(guard.generation);
    setApi(null);
    setIdentity(null);
    setMarket(null);
    setLastReadAt(null);
    setLoginError(null);
    setDemoStaffId(defaultDemoStaffId);
    setLoginNotice("已退出。登录凭证、队列、详情、附件预览和聊天记录均已清除。");
  }

  function changeMarket(next: Market) {
    if (identity === null || market === next) return;
    guard.bump();
    setGeneration(guard.generation);
    setMarket(next);
    setLastReadAt(null);
  }

  function changeMode(next: WorkspaceMode) {
    if (mode === next) return;
    guard.bump();
    setGeneration(guard.generation);
    setMode(next);
    setLastReadAt(null);
  }

  const canSupport = identity?.roles.includes("support") ?? false;
  const canApprove = identity?.roles.includes("approver") ?? false;
  const canAudit = identity?.roles.includes("auditor") ?? false;
  const canOperate = identity?.roles.includes("operator") ?? false;
  const canBusiness = canSupport || canApprove || canAudit;
  const availableModes: WorkspaceMode[] = [
    ...(canBusiness ? (["business"] as const) : []),
    ...(canOperate ? (["operations"] as const) : []),
  ];

  // All hooks stay above every conditional return. The memo keeps the same
  // context object across read-stamp rerenders so read effects do not loop.
  const contextValue: StaffContextValue | null = useMemo(() => {
    if (identity === null || market === null || api === null) return null;
    return {
      api,
      identity,
      market,
      generation,
      isCurrent: (value: number) => guard.isCurrent(value),
      run,
      reportSessionEnding,
      markRead,
      canSupport,
      canApprove,
      canAudit,
      canOperate,
    };
  }, [
    api,
    canApprove,
    canAudit,
    canOperate,
    canSupport,
    generation,
    guard,
    identity,
    market,
    markRead,
    reportSessionEnding,
    run,
  ]);

  if (identity === null || api === null) {
    return (
      <div className="staff-app staff-login-page">
        <div className="staff-login-card">
          <div className="staff-login-brand">
            <span className="staff-brand-mark"><ShieldCheck size={22} aria-hidden="true" /></span>
            <div>
              <h1>售后员工工作台</h1>
              <p>
                {demoMode
                  ? "选择一个合成员工身份并使用演示密码登录"
                  : "使用管理员发放的员工凭证登录"}
              </p>
            </div>
          </div>
          {loginNotice ? <Notice tone="info" title={loginNotice} live /> : null}
          {loginOptionsNotice ? <Notice tone="info" title={loginOptionsNotice} /> : null}
          {demoMode ? (
            <form
              onSubmit={(event: FormEvent<HTMLFormElement>) => {
                event.preventDefault();
                const data = new FormData(event.currentTarget);
                const staffId = data.get("staff_id");
                const password = data.get("password");
                void login({
                  kind: "demo",
                  staffId:
                    typeof staffId === "string" && staffId
                      ? staffId
                      : selectedDemoStaffId,
                  password: typeof password === "string" ? password : "",
                });
              }}
            >
              <label className="staff-field">
                <span className="staff-field-label">演示员工身份</span>
                <select
                  name="staff_id"
                  value={selectedDemoStaffId}
                  onChange={(event) => setDemoStaffId(event.target.value)}
                  disabled={loginBusy}
                  aria-describedby="staff-demo-hint"
                >
                  {demoAccounts.map((account) => (
                    <option value={account.staff_id} key={account.staff_id}>
                      {account.display_name} · {account.roles.map((role) => ROLE_LABELS[role]).join(" / ")}
                    </option>
                  ))}
                </select>
              </label>
              <label className="staff-field">
                <span className="staff-field-label">演示密码</span>
                <input
                  name="password"
                  type="password"
                  autoComplete="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  aria-describedby="staff-demo-hint"
                  required
                />
              </label>
              {loginError ? <Notice tone="danger" title={loginError} /> : null}
              <p className="staff-login-hint" id="staff-demo-hint">
                演示密码只用于换取短时登录凭证；凭证只保存在当前页面内存，退出后需重新选择身份。
                不会写入 localStorage、URL 或日志。
              </p>
              <button className="staff-button staff-button-primary staff-login-submit" type="submit" disabled={loginBusy}>
                <KeyRound size={16} aria-hidden="true" />
                {loginBusy ? "正在验证身份…" : "登录工作台"}
              </button>
            </form>
          ) : (
            <form
              onSubmit={(event: FormEvent<HTMLFormElement>) => {
                event.preventDefault();
                const input = new FormData(event.currentTarget).get("token");
                void login({
                  kind: "token",
                  token: typeof input === "string" ? input : "",
                });
              }}
            >
              <label className="staff-field">
                <span className="staff-field-label">员工凭证（Bearer token）</span>
                <input
                  name="token"
                  type="password"
                  autoComplete="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="粘贴一次性员工凭证"
                  aria-describedby="staff-token-hint"
                  required
                />
              </label>
              {loginError ? <Notice tone="danger" title={loginError} /> : null}
              <p className="staff-login-hint" id="staff-token-hint">
                凭证只保存在当前页面内存，刷新后需重新登录。不会写入 localStorage、URL 或日志。
              </p>
              <button className="staff-button staff-button-primary staff-login-submit" type="submit" disabled={loginBusy}>
                <KeyRound size={16} aria-hidden="true" />
                {loginBusy ? "正在验证身份…" : "登录工作台"}
              </button>
            </form>
          )}
          <a className="staff-link" href="/">返回旧版演示聊天</a>
        </div>
      </div>
    );
  }

  if (market === null) {
    return (
      <div className="staff-app staff-login-page">
        <div className="staff-login-card">
          <Notice tone="warning" title="当前身份没有可访问的市场权限">
            请让管理员为该员工补充至少一个允许市场后重新登录。
          </Notice>
          <button className="staff-button" type="button" onClick={logout}>退出</button>
        </div>
      </div>
    );
  }

  if (availableModes.length === 0) {
    return (
      <div className="staff-app staff-login-page">
        <div className="staff-login-card">
          <EmptyBlock title="当前凭证没有可用的工作台角色">
            需要 support、approver、auditor 或 operator 之一。
          </EmptyBlock>
          <button className="staff-button" type="button" onClick={logout}>退出</button>
        </div>
      </div>
    );
  }

  if (contextValue === null) return null;

  return (
    <StaffContext.Provider value={contextValue}>
      <div className="staff-app">
        <header className="staff-topbar">
          <div className="staff-topbar-brand">
            <span className="staff-brand-mark"><ShieldCheck size={19} aria-hidden="true" /></span>
            <div>
              <strong>售后员工工作台</strong>
              <small>E-pig 内部演示政策 · 员工身份由服务器判定</small>
            </div>
          </div>
          {availableModes.length > 1 ? (
            <nav className="staff-mode-tabs" aria-label="工作台模块">
              {availableModes.map((item) => (
                <button
                  key={item}
                  type="button"
                  className={mode === item ? "active" : ""}
                  aria-current={mode === item ? "page" : undefined}
                  onClick={() => changeMode(item)}
                >
                  {item === "business" ? "业务工作台" : "执行对账"}
                </button>
              ))}
            </nav>
          ) : null}
          <div className="staff-topbar-right">
            <div className="staff-identity">
              <strong>{identity.display_name}</strong>
              <span>
                {ROLE_ORDER.filter((role) => identity.roles.includes(role))
                  .map((role) => ROLE_LABELS[role])
                  .join(" / ")}
              </span>
            </div>
            <label className="staff-market-select">
              <span>市场</span>
              <select
                value={market}
                onChange={(event) => changeMarket(event.target.value as Market)}
                aria-label="选择授权市场"
              >
                {identity.allowed_markets.map((item) => (
                  <option value={item} key={item}>{MARKET_LABELS[item]}</option>
                ))}
              </select>
            </label>
            <span className="staff-read-stamp" title="最近一次成功读取服务端数据的时间">
              数据读取：{lastReadAt ? formatDateTime(lastReadAt) : "尚未读取"}
            </span>
            <button className="staff-button staff-button-quiet" type="button" onClick={logout}>
              <LogOut size={15} aria-hidden="true" />退出
            </button>
          </div>
        </header>

        <main className="staff-main">
          {mode === "operations" ? (
            <OperatorPanel key={`operations:${market}:${generation}`} />
          ) : (
            <StaffWorkbench key={`business:${market}:${generation}`} />
          )}
        </main>
      </div>
    </StaffContext.Provider>
  );
}
