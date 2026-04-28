
import React, { useEffect, useState, useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { UserRole, User } from "@/contexts/AuthContext";
import { useUser } from "@/contexts/user/UserContext";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { toast } from "sonner";
import { ChevronLeft } from "lucide-react";
import { AvatarEditor } from "@/components/users/UserFormComponents/AvatarEditor";
import { UserDetailsForm } from "@/components/users/UserFormComponents/UserDetailsForm";
import { RoleAndManagerSection } from "@/components/users/UserFormComponents/RoleAndManagerSection";
import { AvatarConfig } from "@/components/users/types";
import { features } from "@/config/features";
import type { AvatarFeatures } from "@/components/users/types";

const UserForm: React.FC = () => {
  // Hooks and context
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { users, currentUser, addUser, updateUser, updateAvatar } = useUser();
  
  // Derived state
  const isEditing = id;
  const userToEdit = isEditing ? users.find(user => user.id === id) : null;
  const isSelfEdit = userToEdit?.id === currentUser?.id;
  const availableManagers = users.filter(user => user.role === "manager");
  const canEditRoles = currentUser?.role === "admin" || 
                    (currentUser?.role === "manager" && !isSelfEdit);
  
  // Check if the current user can edit the form
  const canEditUser = useCallback(() => {
    // Admins can do anything
    if (currentUser?.role === "admin") {
      console.log('User is admin, can edit');
      return true;
    }
    
    // Managers can edit themselves, their subordinates, or create new users
    if (currentUser?.role === "manager") {
      // If editing, check if it's themselves or their subordinate
      if (isEditing && userToEdit) {
        const canEdit = userToEdit.id === currentUser.id || userToEdit.managerId === currentUser.id;
        console.log('Manager editing check', { 
          isEditing, 
          editingSelf: userToEdit.id === currentUser.id,
          editingSubordinate: userToEdit.managerId === currentUser.id,
          canEdit
        });
        return canEdit;
      }
      // If creating, they can always create new users (will be their subordinates)
      console.log('Manager can create new user');
      return true;
    }
    
    // Regular users can only edit themselves (handled by isSelfEditByNonManager)
    console.log('Regular user, can only edit self if isSelfEditByNonManager');
    return false;
  }, [currentUser, isEditing, userToEdit]);
  
  const isSelfEditByNonManager = isSelfEdit && 
                               currentUser?.role !== "admin" && 
                               currentUser?.role !== "manager";
                               
  // Check if the form can be edited based on user role and permissions
  const canEditForm = canEditUser() || isSelfEditByNonManager;
  
  const [isDragging, setIsDragging] = useState(false);
  const [formData, setFormData] = useState({
    name: "",
    email: "",
    role: "new-hire" as UserRole,
    managerId: "" as string | undefined,
    avatar: "",
  });
  const [formErrors, setFormErrors] = useState<{manager?: string}>({});
  
  // Avatar configuration state
  const [avatarConfig, setAvatarConfig] = useState<AvatarConfig>(() => {
    // Initialize with default values from features
    const defaultConfig: AvatarConfig = {
      seed: Math.random().toString(36).substring(2, 10),
      customFile: null,
      manualMode: false,
      skinColor: features.avatarFeatures.skinColor[0],
      hairColor: features.avatarFeatures.hairColor[0],
      topType: features.avatarFeatures.topType[0],
      accessoriesType: features.avatarFeatures.accessoriesType[0],
      facialHairType: features.avatarFeatures.facialHairType[0],
      facialHairColor: features.avatarFeatures.facialHairColor[0],
      eyeType: features.avatarFeatures.eyeType[0],
      eyebrowType: features.avatarFeatures.eyebrowType[0],
      mouthType: features.avatarFeatures.mouthType[0],
      clotheType: features.avatarFeatures.clotheType[0],
      clotheColor: features.avatarFeatures.clotheColor[0],
      graphicType: features.avatarFeatures.graphicType[0],
      noseType: features.avatarFeatures.noseType[0],
      hatColor: features.avatarFeatures.hatColor[0]
    };
    
    return defaultConfig;
  });

  // Track loading state
  const [isLoading, setIsLoading] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);

  // Load user data if editing, or set default values for new users
  useEffect(() => {
    if (isEditing) {
      if (userToEdit) {
        setFormData(prev => ({
          ...prev,
          name: userToEdit.name || "",
          email: userToEdit.email || "",
          role: userToEdit.role || "new-hire",
          managerId: userToEdit.managerId || "",
          avatar: userToEdit.avatar || `https://api.dicebear.com/7.x/avataaars/svg?seed=${Math.random().toString(36).substring(7)}`,
        }));
        setIsLoading(false);
      } else if (id) {
        // User not found
        toast.error("User not found");
        navigate("/users");
      }
    } else {
      // Set default values for new user
      const newSeed = Math.random().toString(36).substring(7);
      setFormData(prev => ({
        ...prev,
        name: "",
        email: "",
        role: "new-hire",
        managerId: "",
        avatar: `https://api.dicebear.com/7.x/avataaars/svg?seed=${newSeed}`
      }));
      setIsLoading(false);
    }
  }, [isEditing, userToEdit?.id, id, navigate]);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    setFormData(prev => ({ ...prev, [name]: value }));
  };

  const handleRoleChange = (value: string) => {
    setFormData(prev => ({ ...prev, role: value as UserRole }));
  };
  
  const handleManagerChange = (value: string) => {
    setFormData(prev => ({ ...prev, managerId: value === "none" ? undefined : value }));
  };

  const validateForm = () => {
    const errors: {manager?: string} = {};
    
    // If current user is a manager and creating a new user, auto-set them as the manager
    if (!isEditing && currentUser?.role === 'manager' && 
        (formData.role === 'mentor' || formData.role === 'new-hire') && 
        !formData.managerId) {
      setFormData(prev => ({ ...prev, managerId: currentUser.id }));
      return true;
    }
    
    // For all other cases, validate manager is selected for Mentor and New Hire roles
    if ((formData.role === 'mentor' || formData.role === 'new-hire') && !formData.managerId) {
      errors.manager = 'A manager is required for this role';
    }
    
    setFormErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    console.log('Form submitted', { 
      isEditing, 
      currentUser: currentUser?.role,
      formData,
      canEditUser: canEditUser(),
      isSelfEditByNonManager,
      isSubmitting
    });
    
    // For self-editing non-admin/non-manager users, only validate if avatar is present
    if (isSelfEditByNonManager) {
      if (!formData.avatar) {
        toast.error('Please select an avatar');
        return;
      }
      // Skip other validations for avatar updates
    } else if (!validateForm()) {
      console.log('Form validation failed');
      return;
    }
    
    try {
      setIsSubmitting(true);
      
      // Prepare user data
      let userData;
      
      if (isEditing) {
        if (isSelfEditByNonManager) {
          // For self-editing by non-admin/non-manager users, only update avatar
          console.log('Updating avatar for self', { avatar: formData.avatar });
          if (formData.avatar) {
            await updateAvatar(id!, formData.avatar);
            toast.success("Avatar updated successfully");
            navigate(-1);
            return;
          }
        } else {
          // Update existing user
          userData = {
            ...formData,
            id: id!,
            // For existing users, keep their current manager if not specified
            managerId: formData.managerId || userToEdit?.managerId || undefined
          };
          
          await updateUser(id!, userData);
          toast.success("User updated successfully");
        }
      } else {
        // For new users
        userData = {
          ...formData,
          // If current user is manager, set them as the manager for new users
          managerId: currentUser?.role === 'manager' 
            ? currentUser.id 
            : formData.managerId || undefined
        };
        
        console.log('Creating new user with data:', userData);
        await addUser(userData);
        toast.success("User created successfully");
      }
      
      // Redirect to users list after successful submission
      navigate("/users");
    } catch (error) {
      console.error("Error saving user:", error);
      toast.error("An error occurred while saving the user");
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleAvatarChange = useCallback((url: string) => {
    setFormData(prev => ({ ...prev, avatar: url }));
  }, []);

  const handleFileUpload = useCallback((file: File) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      setFormData(prev => ({ ...prev, avatar: reader.result as string }));
    };
    reader.readAsDataURL(file);
  }, []);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-primary"></div>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-8">
      <div className="mb-6">
        <Button 
          variant="ghost" 
          size="sm" 
          onClick={() => navigate(-1)}
          className="flex items-center gap-2"
        >
          <ChevronLeft className="h-4 w-4" />
          Back to Users
        </Button>
      </div>
      
      <Card>
        <CardHeader>
          <CardTitle>{isEditing ? `Edit User${userToEdit ? `: ${userToEdit.name}` : ''}` : 'Create New User'}</CardTitle>
        </CardHeader>
        <form onSubmit={handleSubmit}>
          <CardContent className="space-y-6">
            {/* Avatar Editor - Always visible but may be read-only */}
            <AvatarEditor
              avatarUrl={formData.avatar}
              name={formData.name}
              onAvatarChange={handleAvatarChange}
              onFileUpload={handleFileUpload}
              avatarConfig={avatarConfig}
              setAvatarConfig={setAvatarConfig}
              avatarFeatures={features.avatarFeatures}
              enableAvatarCustomization={features.enableAvatarCustomization}
              disabled={!canEditForm}
            />
            
            {/* User Details Form */}
            <UserDetailsForm
              name={formData.name}
              email={formData.email}
              onNameChange={(e) => setFormData({...formData, name: e.target.value})}
              onEmailChange={(e) => setFormData({...formData, email: e.target.value})}
              disabled={!canEditUser() || (isSelfEdit && currentUser?.role === 'manager')}
            />
            
            {/* Role and Manager Section - Only show if user has permission */}
            {((canEditUser() && !isSelfEdit) || currentUser?.role === 'admin') && (
              <RoleAndManagerSection
                role={formData.role}
                managerId={formData.managerId || ''}
                onRoleChange={(value) => {
                  // If current user is manager and editing themselves, only allow switching to mentor/new-hire
                  if (currentUser?.role === 'manager' && isSelfEdit) {
                    if (value === 'mentor' || value === 'new-hire') {
                      setFormData({...formData, role: value as UserRole});
                    }
                  } else {
                    setFormData({...formData, role: value as UserRole});
                  }
                }}
                onManagerChange={(value) => {
                  // Only admin can change manager
                  if (currentUser?.role === 'admin') {
                    setFormData({...formData, managerId: value});
                    // Clear error when user selects a manager
                    if (value && formErrors.manager) {
                      setFormErrors({...formErrors, manager: undefined});
                    }
                  }
                }}
                currentUserRole={currentUser?.role}
                availableManagers={availableManagers}
                isSelfEdit={isSelfEdit}
                disabled={!canEditUser()}
                error={formErrors.manager}
                isAdmin={currentUser?.role === 'admin'}
              />
            )}
          </CardContent>
          
          <CardFooter className="flex justify-end gap-4">
            <Button type="button" variant="outline" onClick={() => navigate(-1)}>
              Cancel
            </Button>
            <Button 
              type="submit" 
              disabled={
                isLoading || 
                isSubmitting || 
                // Allow submission if:
                // 1. User has edit permissions, OR
                // 2. It's a self-edit by a non-admin/non-manager (avatar update only)
                !(canEditUser() || (isSelfEditByNonManager && formData.avatar))
              }
              onClick={(e) => {
                console.log('Submit button clicked', { 
                  isLoading, 
                  isSubmitting, 
                  canEdit: canEditUser(),
                  isSelfEditByNonManager,
                  hasAvatar: formData.avatar,
                  disabled: isLoading || isSubmitting || !(canEditUser() || (isSelfEditByNonManager && formData.avatar))
                });
              }}
            >
              {isSubmitting ? (
                <>
                  <svg className="animate-spin -ml-1 mr-2 h-4 w-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  {isEditing ? 'Updating...' : 'Creating...'}
                </>
              ) : isEditing ? (
                isSelfEditByNonManager ? 'Update Avatar' : 'Update User'
              ) : (
                'Create User'
              )}
            </Button>
          </CardFooter>
        </form>
      </Card>
    </div>
  );
};

export default UserForm;