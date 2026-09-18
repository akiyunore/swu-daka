import AdminDashboardPage from "./pages/AdminDashboardPage";
import AdminLoginPage from "./pages/AdminLoginPage";
import LoginPage from "./pages/LoginPage";
import TermsPage from "./pages/TermsPage";
import UserDashboardPage from "./pages/UserDashboardPage";
import { adminPagePath } from "./services/adminRuntime";

export default function App() {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const adminPath = adminPagePath();
  if (path === "/terms") {
    return <TermsPage />;
  }
  if (adminPath && path === adminPath) {
    return <AdminLoginPage />;
  }
  if (adminPath && (path === `${adminPath}/dashboard` || path.startsWith(`${adminPath}/dashboard/`))) {
    return <AdminDashboardPage />;
  }
  if (path === "/account" || path.startsWith("/account/")) {
    return <UserDashboardPage />;
  }
  return <LoginPage />;
}
