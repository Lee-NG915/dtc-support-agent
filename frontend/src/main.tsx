import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import StaffApp from "./staff/StaffApp";
import "./styles.css";

const isStaffRoute = window.location.pathname === "/staff" ||
  window.location.pathname.startsWith("/staff/");

// The staff route gets its own component tree: the legacy demo chat hook and
// its localStorage history are never mounted in staff mode.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>{isStaffRoute ? <StaffApp /> : <App />}</ErrorBoundary>
  </StrictMode>,
);
