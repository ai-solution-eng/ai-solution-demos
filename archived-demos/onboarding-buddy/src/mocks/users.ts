import { User } from '@/contexts/AuthContext';

export const mockUsers: User[] = [
  {
    id: "1",
    name: "Admin User",
    email: "admin@example.com",
    role: "admin",
    avatar: `https://api.dicebear.com/7.x/avataaars/svg?seed=mfgwl6`,
  },
  {
    id: "2",
    name: "Manager User",
    email: "manager@example.com",
    role: "manager",
    avatar: `https://api.dicebear.com/7.x/avataaars/svg?seed=ivfw9`,
  },
  {
    id: "3",
    name: "Mentor User",
    email: "mentor@example.com",
    role: "mentor",
    avatar: `https://api.dicebear.com/7.x/avataaars/svg?seed=rfwxl`,
    managerId: "2",
  },
  {
    id: "4",
    name: "New Hire User",
    email: "newhire@example.com",
    role: "new-hire",
    avatar: `https://api.dicebear.com/7.x/avataaars/svg?seed=yj5ti`,
    managerId: "2",
  },
  {
    id: "5",
    name: "Jan",
    email: "jan@example.com",
    role: "manager",
    avatar: `https://api.dicebear.com/7.x/avataaars/svg?seed=a2s8l`,
  },
  {
    id: "6",
    name: "Claudio",
    email: "claudio@example.com",
    role: "mentor",
    avatar: `https://api.dicebear.com/7.x/avataaars/svg?seed=k4ahx`,
    managerId: "5",
  },
  {
    id: "7",
    name: "Janna",
    email: "janna@example.com",
    role: "new-hire",
    avatar: `https://api.dicebear.com/7.x/avataaars/svg?seed=5blcm`,
    managerId: "5",
  },
];
