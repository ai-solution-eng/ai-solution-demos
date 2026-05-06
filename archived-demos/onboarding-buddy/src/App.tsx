
import { Toaster } from "@/components/ui/toaster";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import { UserProvider } from "./contexts/user";
import { AIConfigProvider } from "./contexts/AIConfigContext";
import { TaskProvider } from "./contexts/task";
import Layout from "./components/layout/Layout";

// Pages
import Login from "./pages/auth/Login";
import Dashboard from "./pages/dashboard/Dashboard";
import UserList from "./pages/users/UserList";
import UserForm from "./pages/users/UserForm";
import TaskTemplateList from "./pages/tasks/TaskTemplateList";
import TaskTemplateForm from "./pages/tasks/TaskTemplateForm";
import UnifiedTaskList from "./pages/tasks/UnifiedTaskList";
import TaskDetailPage from "./pages/tasks/TaskDetailPage";
import AIConfiguration from "./pages/admin/AIConfiguration";
import NotFound from "./pages/NotFound";

const queryClient = new QueryClient();

// Public route component - only accessible when not authenticated
interface PublicRouteProps {
  children: React.ReactNode;
}

const PublicRoute: React.FC<PublicRouteProps> = ({ children }) => {
  const { isAuthenticated } = useAuth();
  
  if (isAuthenticated) {
    return <Navigate to="/dashboard" />;
  }
  
  return <>{children}</>;
};

// Protected route component - only accessible when authenticated
interface ProtectedRouteProps {
  children: React.ReactNode;
  allowedRoles?: string[];
}

const ProtectedRoute: React.FC<ProtectedRouteProps> = ({ children, allowedRoles }) => {
  const { isAuthenticated, user } = useAuth();
  
  if (!isAuthenticated) {
    return <Navigate to="/login" />;
  }
  
  // If roles are specified, check if user has permission
  if (allowedRoles && user && !allowedRoles.includes(user.role)) {
    return <Navigate to="/dashboard" />;
  }
  
  return <>{children}</>;
};

const AppRoutes = () => {
  return (
    <Routes>
      {/* Make login the default route */}
      <Route 
        path="/" 
        element={
          <PublicRoute>
            <Login />
          </PublicRoute>
        } 
      />
      
      {/* Keep login route for direct navigation */}
      <Route 
        path="/login" 
        element={
          <PublicRoute>
            <Login />
          </PublicRoute>
        } 
      />
      
      {/* Protected routes */}
      <Route 
        path="/dashboard" 
        element={
          <ProtectedRoute>
            <Dashboard />
          </ProtectedRoute>
        } 
      />
      
      <Route 
        path="/users" 
        element={
          <ProtectedRoute allowedRoles={["admin", "manager"]}>
            <UserList />
          </ProtectedRoute>
        } 
      />
      
      <Route 
        path="/users/new" 
        element={
          <ProtectedRoute allowedRoles={["admin", "manager"]}>
            <UserForm />
          </ProtectedRoute>
        } 
      />
      
      <Route 
        path="/users/:id" 
        element={
          <ProtectedRoute>
            <UserForm />
          </ProtectedRoute>
        } 
      />
      
      <Route 
        path="/task-templates" 
        element={
          <ProtectedRoute allowedRoles={["admin", "manager"]}>
            <TaskTemplateList />
          </ProtectedRoute>
        } 
      />
      
      <Route 
        path="/task-templates/new" 
        element={
          <ProtectedRoute allowedRoles={["admin", "manager"]}>
            <TaskTemplateForm />
          </ProtectedRoute>
        } 
      />
      
      <Route 
        path="/task-templates/:id" 
        element={
          <ProtectedRoute allowedRoles={["admin", "manager"]}>
            <TaskTemplateForm />
          </ProtectedRoute>
        } 
      />
      
      <Route 
        path="/tasks" 
        element={
          <ProtectedRoute allowedRoles={["admin", "manager", "mentor", "new-hire"]}>
            <UnifiedTaskList />
          </ProtectedRoute>
        } 
      />
      
      <Route 
        path="/tasks/:id" 
        element={
          <ProtectedRoute>
            <TaskDetailPage />
          </ProtectedRoute>
        } 
      />
      
      <Route 
        path="/admin/ai-configuration" 
        element={
          <ProtectedRoute allowedRoles={["admin"]}>
            <AIConfiguration />
          </ProtectedRoute>
        } 
      />
      
      {/* Catch-all route */}
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}

const App = () => (
  <QueryClientProvider client={queryClient}>
    <AuthProvider>
      <UserProvider>
        <AIConfigProvider>
          <TaskProvider>
            <TooltipProvider>
              <BrowserRouter>
                <Layout>
                  <AppRoutes />
                </Layout>
              </BrowserRouter>
              <Toaster />
              <Sonner />
            </TooltipProvider>
          </TaskProvider>
        </AIConfigProvider>
      </UserProvider>
    </AuthProvider>
  </QueryClientProvider>
);

export default App;
