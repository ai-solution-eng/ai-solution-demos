import React from 'react';
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { User, UserRole } from '@/contexts/AuthContext';
import { features } from '@/config/features';
import { useAuth } from '@/contexts/AuthContext';

interface RoleAndManagerSectionProps {
  role: UserRole;
  managerId: string | undefined;
  onRoleChange: (value: string) => void;
  onManagerChange: (value: string) => void;
  currentUserRole: UserRole | undefined;
  availableManagers: User[];
  isSelfEdit: boolean;
  disabled?: boolean;
  error?: string;
  isAdmin?: boolean;
}

export const RoleAndManagerSection: React.FC<RoleAndManagerSectionProps> = ({
  role,
  managerId,
  onRoleChange,
  onManagerChange,
  currentUserRole,
  availableManagers,
  isSelfEdit,
  disabled = false,
  error,
  isAdmin = false
}) => {
  const { user: currentUser } = useAuth();
  
  // Check if the current user can edit roles
  const canEditRoles = isAdmin || 
                    (currentUserRole === 'manager' && !isSelfEdit);
  
  // Check if the current user can assign managers
  const canAssignManager = isAdmin && 
                         (role === 'mentor' || role === 'new-hire') &&
                         !isSelfEdit;
  
  // If current user is a manager, they can only assign themselves as manager
  const managerOptions = currentUserRole === 'manager' && currentUser
    ? availableManagers.filter(manager => manager.id === currentUser.id)
    : availableManagers;

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="role">Role</Label>
        <Select
          value={role}
          onValueChange={onRoleChange}
          disabled={!canEditRoles || isSelfEdit || disabled}
        >
          <SelectTrigger>
            <SelectValue placeholder="Select a role" />
          </SelectTrigger>
          <SelectContent>
            {currentUserRole === "admin" && (
              <SelectItem value="admin">Admin</SelectItem>
            )}
            {currentUserRole === "admin" && (
              <SelectItem value="manager">Manager</SelectItem>
            )}
            {(currentUserRole === "admin" || currentUserRole === "manager") && (
              <SelectItem value="mentor">Mentor</SelectItem>
            )}
            {(currentUserRole === "admin" || currentUserRole === "manager") && (
              <SelectItem value="new-hire">New Hire</SelectItem>
            )}
          </SelectContent>
        </Select>
        
        <p className="text-xs text-muted-foreground mt-1">
          {role === 'admin' && 'Full access to the platform, including user management and all features.'}
          {role === 'manager' && 'Can manage new hires, mentors, task templates, and assignments.'}
          {role === 'mentor' && 'Reviews completed tasks and provides feedback to new hires.'}
          {role === 'new-hire' && 'Completes assigned onboarding tasks and views progress.'}
        </p>
      </div>
      
      {/* Manager assignment for admin and manager users when editing mentor and new-hire roles */}
      {canAssignManager && (
        <div className="space-y-2">
          <Label htmlFor="manager">Assign to Manager</Label>
          <Select
            value={managerId || ""}
            onValueChange={onManagerChange}
            disabled={disabled || (currentUserRole === 'manager' && managerId !== currentUser?.id)}
            required
          >
            <SelectTrigger className={(!managerId ? "border-destructive" : "") + (error ? " border-red-500" : "")}>
              <SelectValue placeholder="Select a manager" />
            </SelectTrigger>
            <SelectContent>
              {managerOptions.length > 0 ? (
                managerOptions.map((manager) => (
                  <SelectItem key={manager.id} value={manager.id}>
                    {manager.name}
                  </SelectItem>
                ))
              ) : (
                <div className="text-sm text-muted-foreground p-2">
                  {currentUserRole === 'manager' 
                    ? 'You need to be assigned as a manager to manage users'
                    : 'No managers available'}
                </div>
              )}
            </SelectContent>
          </Select>
          {error && (
            <p className="text-xs text-destructive mt-1">
              {error}
            </p>
          )}
          <p className="text-xs text-muted-foreground mt-1">
            {managerOptions.length === 0 
              ? currentUserRole === 'manager'
                ? 'You need to be assigned as a manager to manage users'
                : 'No managers available. Please contact an admin to create a manager account.'
              : currentUserRole === 'manager' && managerId !== currentUser?.id
                ? 'You can only assign users to yourself as a manager'
                : 'The manager who will oversee this user\'s work'}
          </p>
        </div>
      )}
    </div>
  );
};
