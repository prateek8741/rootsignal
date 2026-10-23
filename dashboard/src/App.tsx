import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { RequireConnection } from "./components/Layout";
import { ConnectionProvider } from "./lib/connection";
import { ConnectPage } from "./pages/ConnectPage";
import { IssueDetailPage } from "./pages/IssueDetailPage";
import { IssuesPage } from "./pages/IssuesPage";
import { TraceDetailPage } from "./pages/TraceDetailPage";
import { TracesPage } from "./pages/TracesPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/connect" element={<ConnectPage />} />
      <Route element={<RequireConnection />}>
        <Route path="/traces" element={<TracesPage />} />
        <Route path="/traces/:traceId" element={<TraceDetailPage />} />
        <Route path="/issues" element={<IssuesPage />} />
        <Route path="/issues/:issueId" element={<IssueDetailPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/traces" replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <ConnectionProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </ConnectionProvider>
  );
}
