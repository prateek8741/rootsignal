import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "./components/Layout";
import { AuthProvider } from "./lib/auth";
import { CaseDetailPage } from "./pages/CaseDetailPage";
import { IssueDetailPage } from "./pages/IssueDetailPage";
import { IssuesPage } from "./pages/IssuesPage";
import { LinkLoginPage } from "./pages/LinkLoginPage";
import { LoginPage } from "./pages/LoginPage";
import { TraceDetailPage } from "./pages/TraceDetailPage";
import { TracesPage } from "./pages/TracesPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/login/link" element={<LinkLoginPage />} />
      <Route element={<RequireAuth />}>
        <Route path="/traces" element={<TracesPage />} />
        <Route path="/traces/:traceId" element={<TraceDetailPage />} />
        <Route path="/issues" element={<IssuesPage />} />
        <Route path="/issues/:issueId" element={<IssueDetailPage />} />
        <Route path="/cases/:caseId" element={<CaseDetailPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/traces" replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </AuthProvider>
  );
}
