import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Login } from "./pages/Login";
import { Picker } from "./pages/Picker";
import { Project } from "./pages/Project";
import { Vision } from "./pages/Vision";
import { Feedback } from "./pages/Feedback";
import { Sessions } from "./pages/Sessions";
import { TaskDetail } from "./pages/TaskDetail";
import { Runs } from "./pages/Runs";
import { RunLog } from "./pages/RunLog";
import { Messages } from "./pages/Messages";
import { Scheduler } from "./pages/Scheduler";
import { Autonomous } from "./pages/Autonomous";
import { Workflow } from "./pages/Workflow";
import { Help } from "./pages/Help";
import { Users } from "./pages/Users";
import { SystemSettings } from "./pages/SystemSettings";
import { Shell } from "./components/Shell";

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Login is the only route fully outside the sidebar shell */}
        <Route path="/login" element={<Login />} />

        {/* Everything else inside the Shell sidebar layout */}
        <Route element={<Shell />}>
          <Route path="/" element={<Picker />} />
          <Route path="/help" element={<Help />} />
          <Route path="/p/:slug" element={<Project />} />
          <Route path="/p/:slug/t/:id" element={<TaskDetail />} />
          <Route path="/p/:slug/vision" element={<Vision />} />
          <Route path="/p/:slug/workflow" element={<Workflow />} />
          <Route path="/p/:slug/feedback" element={<Feedback />} />
          <Route path="/p/:slug/sessions" element={<Sessions />} />
          <Route path="/p/:slug/runs" element={<Runs />} />
          <Route path="/p/:slug/runs/:id" element={<RunLog />} />
          <Route path="/p/:slug/sessions/:claude_uuid/messages" element={<Messages />} />
          <Route path="/p/:slug/autonomous" element={<Autonomous />} />
          <Route path="/scheduler" element={<Scheduler />} />
          <Route path="/users" element={<Users />} />
          <Route path="/system-settings" element={<SystemSettings />} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
