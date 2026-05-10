import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Login } from "./pages/Login";
import { Picker } from "./pages/Picker";
import { Project } from "./pages/Project";
import { Vision } from "./pages/Vision";
import { Feedback } from "./pages/Feedback";

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<Picker />} />
        <Route path="/p/:slug" element={<Project />} />
        <Route path="/p/:slug/vision" element={<Vision />} />
        <Route path="/p/:slug/feedback" element={<Feedback />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
