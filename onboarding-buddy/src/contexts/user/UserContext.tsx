import React, { createContext, useContext, useMemo, useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
// Remove useAuth import since we're using AuthContext directly
import { UserContextType } from './types';
import { User, UserRole, AuthContext } from '@/contexts/AuthContext';

// Import shared mock users
import { mockUsers } from '@/mocks/users';

const UserContext = createContext<UserContextType | undefined>(undefined);

// Custom hook to use the user context
export const useUser = (): UserContextType => {
  const context = useContext(UserContext);
  if (context === undefined) {
    throw new Error('useUser must be used within a UserProvider');
  }
  return context;
};

export const UserProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const authContext = useContext(AuthContext);
  
  if (!authContext) {
    throw new Error('UserProvider must be used within an AuthProvider');
  }
  
  const { 
    user: currentUser,
    users: authUsers = [],
    updateCurrentUser,
    updateUsers,
    isImpersonating = false,
    impersonatedUser: authImpersonatedUser = null,
    impersonateUser: authImpersonateUser = () => {},
    stopImpersonation: authStopImpersonation = () => {},
  } = authContext;

  const [users, setUsers] = useState<User[]>(authUsers || mockUsers);
  const [isLoading, setIsLoading] = useState<boolean>(false);

  // Sync with auth context users - only when authUsers changes
  useEffect(() => {
    if (authUsers && authUsers.length > 0) {
      // Only update if the users are actually different
      if (JSON.stringify(authUsers) !== JSON.stringify(users)) {
        setUsers(authUsers);
      }
    }
  }, [authUsers]); // Removed users and updateUsers from dependencies

  // Update both local and auth context users
  const updateUsersList = useCallback((newUsers: User[]) => {
    // Update local state
    setUsers(newUsers);
    // Update auth context which will handle localStorage
    updateUsers(newUsers);
  }, [updateUsers]);

  const addUser = useCallback((userData: Omit<User, 'id'>) => {
    try {
      console.log('Adding new user with data:', userData);
      
      // Check if user with this email already exists
      const existingUser = users.find(user => user.email.toLowerCase() === userData.email.toLowerCase());
      if (existingUser) {
        throw new Error('A user with this email already exists');
      }

      // Use the provided avatar URL or fall back to an empty string
      // The form should have already provided a generated avatar
      const avatarUrl = userData.avatar || '';

      const newUser = {
        ...userData,
        id: Math.random().toString(36).substring(2, 9), // Simple ID generation
        avatar: avatarUrl,
      };
      
      console.log('Created new user:', newUser);
      
      const updatedUsers = [...users, newUser];
      updateUsersList(updatedUsers);
      
      // Update localStorage
      localStorage.setItem('users', JSON.stringify(updatedUsers));
      
      toast.success(`User ${newUser.name} added successfully`);
      return newUser;
    } catch (error) {
      console.error('Error creating user:', error);
      toast.error(error instanceof Error ? error.message : 'Failed to create user');
      throw error;
    }
  }, [users, updateUsersList]);

  // Get all subordinates for a manager
  const getSubordinates = useCallback((managerId: string): User[] => {
    return users.filter(user => user.managerId === managerId);
  }, [users]);

  const updateUser = useCallback((id: string, data: Partial<User>) => {
    try {
      console.log('Updating user with data:', { id, data });
      
      // Find the existing user to ensure we have all required fields
      const existingUser = users.find(u => u.id === id);
      if (!existingUser) {
        throw new Error('User not found');
      }

      // Prevent duplicate emails
      if (data.email && data.email !== existingUser.email) {
        console.log('Email is being changed, checking for duplicates...');
        console.log('Current email:', existingUser.email);
        console.log('New email:', data.email);
        
        const emailExists = users.some(user => {
          const isDuplicate = user.email.toLowerCase() === data.email?.toLowerCase() && user.id !== id;
          if (isDuplicate) {
            console.log('Found duplicate email for user:', user);
          }
          return isDuplicate;
        });
        
        if (emailExists) {
          console.error('Email already exists for another user');
          throw new Error('A user with this email already exists');
        } else {
          console.log('Email is available');
        }
      }

      // Ensure we have a valid avatar URL
      let updatedData = { ...data };
      // Only generate a new avatar if this is a new user or avatar is explicitly being cleared
      if (!existingUser.avatar && (!updatedData.avatar || updatedData.avatar.trim() === '')) {
        // If no avatar exists and none is provided, generate a new one
        const seed = existingUser.name?.replace(/\s+/g, '') || 
                    existingUser.email?.split('@')[0] || 
                    Math.random().toString(36).substring(7);
        updatedData.avatar = `https://api.dicebear.com/7.x/avataaars/svg?seed=${seed}`;
      } else if (existingUser.avatar && !updatedData.avatar) {
        // If user already has an avatar and it's not being changed, keep the existing one
        updatedData.avatar = existingUser.avatar;
      }

      // Ensure required fields are not removed
      const updatedUser = {
        ...existingUser,
        ...updatedData,
        // Ensure these required fields are always set
        name: updatedData.name || existingUser.name,
        email: updatedData.email || existingUser.email,
        role: updatedData.role || existingUser.role,
        // Update the updatedAt timestamp
        updatedAt: new Date().toISOString()
      };

      const updatedUsers = users.map(user =>
        user.id === id ? updatedUser : user
      );
      
      updateUsersList(updatedUsers);
      
      // If the updated user is the current user, update the current user in auth context
      if (currentUser?.id === id) {
        console.log('Updating current user:', updatedUser);
        updateCurrentUser(updatedUser);
      }

      toast.success('User updated successfully');
      return updatedUser;
    } catch (error) {
      console.error('Error updating user:', error);
      toast.error(error instanceof Error ? error.message : 'Failed to update user');
      throw error;
    }
  }, [users, currentUser, updateCurrentUser, updateUsersList]);

  const deleteUser = useCallback((userId: string) => {
    try {
      // Check if user has subordinates
      const hasSubordinates = users.some(user => user.managerId === userId);
      if (hasSubordinates) {
        throw new Error('Cannot delete user with subordinates');
      }

      const updatedUsers = users.filter(user => user.id !== userId);
      updateUsersList(updatedUsers);
      toast.success('User deleted successfully');
    } catch (error) {
      console.error('Error deleting user:', error);
      toast.error(error instanceof Error ? error.message : 'Failed to delete user');
      throw error;
    }
  }, [users, updateUsersList]);



  // Sync with auth context users
  useEffect(() => {
    if (authUsers && authUsers.length > 0) {
      // Only update if the users are actually different
      if (JSON.stringify(authUsers) !== JSON.stringify(users)) {
        setUsers(authUsers);
      }
    }
  }, [authUsers, users]);

  const canManageUser = useCallback((targetUserId: string): boolean => {
    if (!currentUser) return false;
    
    // Admins can manage anyone except themselves (for demos)
    if (currentUser.role === 'admin') {
      return currentUser.id !== targetUserId;
    }
    
    // Managers can manage their direct reports and themselves
    if (currentUser.role === 'manager') {
      const targetUser = users.find(u => u.id === targetUserId);
      return targetUser ? (targetUser.managerId === currentUser.id || targetUser.id === currentUser.id) : false;
    }
    
    // Mentors and new-hires can only manage themselves
    return currentUser.id === targetUserId;
  }, [currentUser, users]);

  const getAvailableManagers = useCallback((excludeUserId?: string): User[] => {
    return users.filter(user => 
      (user.role === 'admin' || user.role === 'manager') && 
      user.id !== excludeUserId
    );
  }, [users]);

  const getManager = useCallback((userId: string): User | undefined => {
    const user = users.find(u => u.id === userId);
    if (!user?.managerId) return undefined;
    return users.find(u => u.id === user.managerId);
  }, [users]);

  const getSubordinatesCount = useCallback((userId: string): number => {
    return users.filter(user => user.managerId === userId).length;
  }, [users]);

  const getUsersByRole = useCallback((role: UserRole): User[] => {
    return users.filter(user => user.role === role);
  }, [users]);

  const updateAvatar = useCallback((userId: string, avatar: string) => {
    try {
      // Update the users list in both UserContext and AuthContext
      const updatedUsers = users.map(user => 
        user.id === userId ? { ...user, avatar } : user
      );
      
      // Update the users list in both contexts
      updateUsersList(updatedUsers);
      
      // Update current user if updating self
      if (currentUser?.id === userId) {
        const updatedUser = { ...currentUser, avatar };
        updateCurrentUser(updatedUser);
        
        // Update the user in localStorage
        localStorage.setItem('user', JSON.stringify(updatedUser));
      } else {
        // If updating another user, still update localStorage if it's the current user
        const storedUser = localStorage.getItem('user');
        if (storedUser) {
          try {
            const parsedUser = JSON.parse(storedUser);
            if (parsedUser.id === userId) {
              localStorage.setItem('user', JSON.stringify({ ...parsedUser, avatar }));
            }
          } catch (e) {
            console.error('Error parsing stored user', e);
          }
        }
      }
      
      toast.success('Avatar updated successfully');
    } catch (error) {
      console.error('Error updating avatar:', error);
      toast.error('Failed to update avatar');
      throw error;
    }
  }, [currentUser, updateCurrentUser]);

  // Handle login - this would be called after successful authentication
  const login = useCallback(async (email: string, password: string): Promise<User> => {
    setIsLoading(true);
    try {
      // Simulate API call
      await new Promise(resolve => setTimeout(resolve, 500));
      
      const user = users.find(u => u.email === email);
      if (!user) {
        throw new Error('User not found');
      }
      
      // In a real app, you would validate the password here
      updateCurrentUser(user);
      return user;
    } catch (error) {
      console.error('Login error:', error);
      throw error;
    } finally {
      setIsLoading(false);
    }
  }, [users, updateCurrentUser]);

  const impersonateUser = useCallback((userId: string) => {
    const user = users.find(u => u.id === userId);
    if (user) {
      authImpersonateUser(user);
    } else {
      console.error(`User with id ${userId} not found`);
    }
  }, [users, authImpersonateUser]);

  const stopImpersonation = useCallback(() => {
    authStopImpersonation();
  }, [authStopImpersonation]);

  const value = useMemo(() => ({
    users,
    currentUser: isImpersonating && authImpersonatedUser ? authImpersonatedUser : currentUser,
    addUser,
    updateUser,
    deleteUser,
    updateAvatar,
    getSubordinates,
    canManageUser,
    getAvailableManagers,
    isImpersonating,
    impersonatedUser: authImpersonatedUser,
    impersonateUser,
    stopImpersonation,
    getManager,
    getSubordinatesCount,
    getUsersByRole,
    login,
    isLoading,
  }), [
    users,
    currentUser,
    authImpersonatedUser,
    addUser,
    updateUser,
    deleteUser,
    updateAvatar,
    canManageUser,
    getAvailableManagers,
    isImpersonating,
    impersonateUser,
    stopImpersonation,
    getManager,
    getSubordinatesCount,
    getUsersByRole,
    login,
    isLoading,
  ]);

  return (
    <UserContext.Provider value={value}>
      {children}
    </UserContext.Provider>
  );
};

export const useUsers = (): UserContextType => {
  const context = useContext(UserContext);
  if (context === undefined) {
    throw new Error('useUsers must be used within a UserProvider');
  }
  return context;
};

export default UserContext;
