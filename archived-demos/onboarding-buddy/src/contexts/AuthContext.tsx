import { createContext, useState, useCallback, ReactNode, useEffect, useContext } from 'react';
import { toast } from 'sonner';
import { mockUsers } from '@/mocks/users';

// Define user roles
export type UserRole = 'admin' | 'manager' | 'mentor' | 'new-hire';

// Define user interface
export interface User {
  id: string;
  name: string;
  email: string;
  role: UserRole;
  avatar?: string;
  managerId?: string;
  completionNotes?: string;
}

interface AuthContextType {
  user: User | null;
  users: User[];
  login: (email: string, password: string) => Promise<User>;
  logout: () => Promise<boolean>;
  isAuthenticated: boolean;
  isLoading: boolean;
  isImpersonating: boolean;
  impersonatedUser: User | null;
  impersonateUser: (user: User) => void;
  stopImpersonation: () => void;
  updateCurrentUser: (user: User) => void;
  updateUsers: (users: User[]) => void;
}

export const AuthContext = createContext<AuthContextType | undefined>(undefined);

interface AuthProviderProps {
  children: ReactNode;
}

export const AuthProvider = ({ children }: AuthProviderProps) => {
  const [user, setUser] = useState<User | null>(null);
  const [isAuthenticated, setIsAuthenticated] = useState<boolean>(false);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [isImpersonating, setIsImpersonating] = useState<boolean>(false);
  const [impersonatedUser, setImpersonatedUser] = useState<User | null>(null);
  const [realUser, setRealUser] = useState<User | null>(null);
  const [users, setUsers] = useState<User[]>([]);

  // Update users list and persist to localStorage
  const updateUsers = useCallback((newUsers: User[]) => {
    setUsers(newUsers);
    // Persist to localStorage
    localStorage.setItem('users', JSON.stringify(newUsers));
  }, []);

  // Update current user in auth context
  const updateCurrentUser = useCallback((updatedUser: User) => {
    setUser(updatedUser);
    // Update the user in the users list
    setUsers(prevUsers => {
      const updatedUsers = prevUsers.map(u => u.id === updatedUser.id ? updatedUser : u);
      // Persist to localStorage
      localStorage.setItem('users', JSON.stringify(updatedUsers));
      return updatedUsers;
    });
  }, []);

  // Initialize user and users from localStorage on mount
  useEffect(() => {
    try {
      // First, try to load users from localStorage
      const storedUsers = localStorage.getItem('users');
      if (storedUsers) {
        const parsedUsers = JSON.parse(storedUsers);
        if (parsedUsers.length > 0) {
          setUsers(parsedUsers);
        } else {
          // If no users in localStorage, use mock users
          setUsers(mockUsers);
          localStorage.setItem('users', JSON.stringify(mockUsers));
        }
      } else {
        // If no users in localStorage, use mock users
        setUsers(mockUsers);
        localStorage.setItem('users', JSON.stringify(mockUsers));
      }

      // Then try to load current user
      const storedUser = localStorage.getItem('currentUser');
      if (storedUser) {
        const parsedUser = JSON.parse(storedUser);
        setUser(parsedUser);
        setIsAuthenticated(true);
      }
    } catch (error) {
      console.error('Error initializing auth state:', error);
      // Fallback to mock users on error
      setUsers(mockUsers);
      localStorage.setItem('users', JSON.stringify(mockUsers));
    }
    // We only want to run this effect once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Login function
  const login = useCallback(async (email: string, password: string): Promise<User> => {
    console.log('[Auth] Attempting login with email:', email);
    setIsLoading(true);
    
    try {
      // Simulate API call
      await new Promise(resolve => setTimeout(resolve, 500));
      
      // Get users from localStorage or use current users state
      const storedUsers = localStorage.getItem('users');
      const userList = storedUsers ? JSON.parse(storedUsers) : users.length > 0 ? users : mockUsers;
      
      // Find user by email (case-insensitive)
      const foundUser = userList.find((user: User) => 
        user.email.toLowerCase() === email.toLowerCase()
      );
      
      if (!foundUser) {
        console.log('[Auth] Login failed: User not found');
        throw new Error('Invalid email or password');
      }
      
      // In a real app, you would verify the password here
      // For demo purposes, we'll just check if the password is not empty
      if (!password) {
        console.log('[Auth] Login failed: No password provided');
        throw new Error('Password is required');
      }
      
      console.log('[Auth] Login successful for user:', foundUser);
      setUser(foundUser);
      setIsAuthenticated(true);
      
      // Store user in localStorage for persistence
      localStorage.setItem('currentUser', JSON.stringify(foundUser));
      
      toast.success(`Welcome back, ${foundUser.name}!`);
      return foundUser;
    } catch (error) {
      console.error('[Auth] Login error:', error);
      toast.error(error instanceof Error ? error.message : 'Login failed');
      throw error;
    } finally {
      setIsLoading(false);
    }
  }, [users]);

  // Logout function
  const logout = useCallback(async (): Promise<boolean> => {
    console.log('[Auth] Logging out...');
    
    try {
      // Clear all auth-related state
      setUser(null);
      setIsAuthenticated(false);
      setIsImpersonating(false);
      setImpersonatedUser(null);
      setRealUser(null);
      
      // Clear user data from localStorage
      localStorage.removeItem('currentUser');
      // Also clear users to force reload from mock data on next login
      localStorage.removeItem('users');
      
      toast.info('You have been logged out');
      return true;
    } catch (error) {
      console.error('Error during logout:', error);
      toast.error('Failed to log out');
      return false;
    }
  }, []);

  // Impersonate user function
  const impersonateUser = useCallback((userToImpersonate: User) => {
    if (!user) return;
    
    setRealUser(user);
    setUser(userToImpersonate);
    setImpersonatedUser(userToImpersonate);
    setIsImpersonating(true);
    
    toast.success(`Now impersonating ${userToImpersonate.name}`);
  }, [user]);

  // Stop impersonation function
  const stopImpersonation = useCallback(() => {
    if (!realUser) return;
    
    setUser(realUser);
    setImpersonatedUser(null);
    setIsImpersonating(false);
    setRealUser(null);
    
    toast.info('Stopped impersonation');
  }, [realUser]);

  // Context value
  const value = {
    user: isImpersonating && impersonatedUser ? impersonatedUser : user,
    users,
    login,
    logout,
    isAuthenticated,
    isLoading,
    isImpersonating,
    impersonatedUser,
    impersonateUser,
    stopImpersonation,
    updateCurrentUser,
    updateUsers,
  };

  // Initialize with mock users if none exist
  useEffect(() => {
    // Only run this effect if users array is empty
    if (users.length === 0) {
      setUsers(mockUsers);
    }
    
    // Check for impersonation flag in URL - only run this once on mount
    const params = new URLSearchParams(window.location.search);
    const impersonateId = params.get('impersonate');
    
    if (impersonateId && user) {
      // Use a ref or state to track if we've already processed impersonation
      const userToImpersonate = users.find(u => u.id === impersonateId) || 
                               mockUsers.find(u => u.id === impersonateId);
      if (userToImpersonate) {
        impersonateUser(userToImpersonate);
      }
    }
    // We only want to run this effect once on mount, so we don't include any dependencies
    // This is safe because we only care about the initial URL parameters
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  
  // Update users in AuthContext when they change in UserContext
  const handleUsersUpdate = useCallback((newUsers: User[]) => {
    // Only update if the users array has actually changed
    if (JSON.stringify(users) !== JSON.stringify(newUsers)) {
      setUsers(newUsers);
    }
  }, [users]);

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
};

// Export useAuth hook for convenience
export const useAuth = (): AuthContextType => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
