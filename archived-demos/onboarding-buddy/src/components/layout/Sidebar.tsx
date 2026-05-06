import React from 'react';
import { LayoutDashboard, Users, BookOpen, BarChart3, User, Settings } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { NavLink } from 'react-router-dom';
import { cn } from '@/lib/utils';
import { useSidebar } from '@/components/ui/sidebar';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';

const Sidebar = () => {
  const { user, isImpersonating } = useAuth();
  const { toggleSidebar, isMobile } = useSidebar();
  
  if (!user) return null;

  // Close sidebar when a link is clicked on mobile
  const handleLinkClick = () => {
    if (isMobile) {
      toggleSidebar();
    }
  };

  // Role-based navigation links
  const navLinks = [
    { to: "/dashboard", label: "Dashboard", icon: LayoutDashboard, roles: ['admin', 'manager', 'mentor', 'new-hire'] },
    { to: "/users", label: "Users", icon: Users, roles: ['admin', 'manager'] },
    { to: "/task-templates", label: "Task Templates", icon: BookOpen, roles: ['admin', 'manager'] },
    { to: "/tasks", label: "Onboarding Tasks", icon: BarChart3, roles: ['admin', 'manager', 'mentor', 'new-hire'] },
    { to: "/admin/ai-configuration", label: "AI Configuration", icon: Settings, roles: ['admin'] },
    { to: `/users/${user.id}`, label: "My Profile", icon: User, roles: ['admin', 'manager', 'mentor', 'new-hire'] },
  ]
    .filter(link => link.roles.includes(user.role))
    .filter(link => !isImpersonating || link.label !== 'My Profile');

  return (
    <div className="h-full flex flex-col bg-sidebar w-full">
      <div className="px-4 py-6">
        <div className="flex items-center space-x-3 mb-6">
          <div className="relative">
            <Avatar className="h-10 w-10 border-2 border-sidebar-accent">
              <AvatarImage src={user.avatar} alt={user.name} />
              <AvatarFallback className="bg-blue-500 text-white">
                {user.name.split(' ').map(n => n[0]).join('').toUpperCase()}
              </AvatarFallback>
            </Avatar>
          </div>
          <div>
            <p className="font-medium text-sidebar-foreground">{user.name}</p>
            <p className="text-sm text-sidebar-muted-foreground capitalize">
              {user.role.replace('-', ' ')}
            </p>
          </div>
        </div>

        <nav className="space-y-1">
          {navLinks.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              end={link.to === '/users'} // Add end prop for exact matching on /users
              onClick={handleLinkClick}
              className={({ isActive }) =>
                cn(
                  'flex items-center px-4 py-2.5 text-sm font-medium rounded-lg mx-2',
                  isActive
                    ? 'bg-blue-600 text-white'
                    : 'text-white hover:bg-blue-50 hover:text-blue-600',
                  'transition-colors duration-200',
                  // Add a group class for better icon styling
                  'group'
                )
              }
            >
              <link.icon className={cn(
                'mr-3 h-5 w-5 flex-shrink-0',
                'text-inherit opacity-80',
                'group-hover:opacity-100',
                'group-[.active]:opacity-100'
              )} />
              {link.label}
            </NavLink>
          ))}
        </nav>
      </div>
    </div>
  );
};

export default Sidebar;