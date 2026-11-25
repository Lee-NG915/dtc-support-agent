import { useEffect, useRef, useState } from "react";
import { ArrowDown, CircleHelp, Menu, PanelLeftOpen } from "lucide-react";
import { checkHealth } from "./lib/api";
import { getAuthMe, logout, type AuthMe } from "./lib/consumer";
import { useChat } from "./hooks/useChat";
import { AccountPanel } from "./components/AccountPanel";
import { Composer } from "./components/Composer";
import { Logo } from "./components/Logo";
import { MessageView } from "./components/MessageView";
import { SearchDialog } from "./components/SearchDialog";
import { Sidebar } from "./components/Sidebar";
import type { ActionProposal } from "./types";

const SUGGESTIONS = [
  { icon: "↗", title: "退货政策", prompt: "定制商品过了七天，但有质量问题还能退吗？" },
  { icon: "◎", title: "物流进度", prompt: "帮我查一下 ORD10001 的物流进度" },
  { icon: "◇", title: "订单查询", prompt: "查一下订单 ORD10001 的状态和金额" },
  { icon: "✳", title: "人工确认", prompt: "取消订单 ORD10003" },
];

function App() {
  const chat = useChat();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const [authMe, setAuthMe] = useState<AuthMe | null>(null);
  const [accountOpen, setAccountOpen] = useState(false);
  const [theme, setTheme] = useState<"light" | "dark">(() => localStorage.getItem("adaptive-rag-theme") === "dark" ? "dark" : "light");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    getAuthMe()
      .then(setAuthMe)
      .catch(() => setAuthMe({ auth_enabled: false, signed_in: false }));
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("adaptive-rag-theme", theme);
  }, [theme]);
  useEffect(() => {
    const controller = new AbortController();
    void checkHealth(controller.signal).then(setHealthy);
    return () => controller.abort();
  }, []);
  useEffect(() => {
    function onKey(event: globalThis.KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setSearchOpen(true); }
      if (event.key === "Escape") { setSearchOpen(false); setSidebarOpen(false); }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  function newChat() {
    chat.select(null);
    setDraft("");
    setSidebarOpen(false);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }

  function selectChat(id: string) { chat.select(id); setSidebarOpen(false); setSearchOpen(false); }
  function suggest(prompt: string) { setDraft(prompt); inputRef.current?.focus(); }
  function confirm(action: ActionProposal, messageId: string) {
    if (!chat.active) return;
    const conversationId = chat.active.id;
    void chat.send("确认执行", action.action_id).then((succeeded) => {
      if (succeeded) chat.dismissAction(conversationId, messageId);
    });
  }

  return <div className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
    <Sidebar conversations={chat.conversations} activeId={chat.active?.id ?? null} open={sidebarOpen} onClose={() => { setSidebarOpen(false); setSidebarCollapsed(true); }} onNew={newChat} onSelect={selectChat} onRemove={chat.remove} onSearch={() => setSearchOpen(true)} onTheme={() => setTheme(theme === "light" ? "dark" : "light")} theme={theme} />
    <main className="main-panel">
      <header className="topbar">
        <button className="icon-button open-sidebar" aria-label="打开侧栏" title="打开侧栏" onClick={() => { setSidebarCollapsed(false); setSidebarOpen(true); }}>{sidebarCollapsed ? <PanelLeftOpen size={20} /> : <Menu size={20} />}</button>
        <span className="model-title">DTC Support Agent</span>
        <div className="topbar-right">{authMe?.auth_enabled && !authMe.signed_in && <a className="auth-link" href="/api/v1/auth/github/login">GitHub 登录</a>}{authMe?.signed_in && <><button className="auth-link" onClick={() => setAccountOpen(true)}>@{authMe.login} · 我的数据</button><button className="auth-link" onClick={() => { void logout().then(() => window.location.reload()); }}>注销</button></>}<span className={`connection-pill ${healthy === false ? "offline" : ""}`}><span />{healthy === null ? "连接中" : healthy ? "已连接" : "服务未连接"}</span><button className="icon-button help-button" title="说明" aria-label="说明" onClick={() => window.open("https://github.com/Lee-NG915/dtc-support-agent", "_blank", "noopener,noreferrer")}><CircleHelp size={19} /></button></div>
      </header>
      {healthy === false && <div className="service-banner" role="status"><strong>暂时无法连接服务</strong><span>请确认后端已启动后重试。</span></div>}
      {chat.active ? <div className="chat-content"><MessageView conversation={chat.active} busy={chat.busy} onConfirm={confirm} onCancel={(messageId) => chat.dismissAction(chat.active!.id, messageId)} /></div>
        : <div className="welcome"><div className="welcome-inner"><Logo /><h1>有什么可以帮忙的？</h1><p>演示订单 ORD10001–10003：查订单、查物流、问退换政策；取消订单需人工确认后执行。</p><div className="suggestions">{SUGGESTIONS.map((item) => <button key={item.title} onClick={() => suggest(item.prompt)}><span className="suggestion-icon">{item.icon}</span><span><strong>{item.title}</strong><small>{item.prompt}</small></span><ArrowDown size={15} /></button>)}</div></div></div>}
      <Composer busy={chat.busy} onSend={(message) => void chat.send(message)} onStop={chat.stop} draft={draft} onDraft={setDraft} inputRef={inputRef} />
    </main>
    {searchOpen && <SearchDialog conversations={chat.conversations} onClose={() => setSearchOpen(false)} onSelect={selectChat} />}
    {accountOpen && authMe?.login && <AccountPanel login={authMe.login} onClose={() => setAccountOpen(false)} />}
  </div>;
}

export default App;
