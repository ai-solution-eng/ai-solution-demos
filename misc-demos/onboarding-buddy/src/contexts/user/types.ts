import { User, UserRole } from "@/contexts/AuthContext";

export interface UserContextType {
  users: User[];
  currentUser: User | null;
  addUser: (user: Omit<User, "id">) => void;
  updateUser: (id: string, data: Partial<User>) => void;
  deleteUser: (id: string) => void;
  getSubordinates: (userId: string) => User[];
  canManageUser: (targetUserId: string) => boolean;
  updateAvatar: (userId: string, avatar: string) => void;
  getAvailableManagers: (excludeUserId?: string) => User[];
  isImpersonating: boolean;
  impersonatedUser: User | null;
  impersonateUser: (userId: string) => void;
  stopImpersonation: () => void;
  getManager: (userId: string) => User | undefined;
  getSubordinatesCount: (userId: string) => number;
  getUsersByRole: (role: UserRole) => User[];
}
