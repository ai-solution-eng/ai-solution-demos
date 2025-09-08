import React from 'react';
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface UserDetailsFormProps {
  name: string;
  email: string;
  onNameChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onEmailChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  disabled?: boolean;
}

export const UserDetailsForm: React.FC<UserDetailsFormProps> = ({
  name,
  email,
  onNameChange,
  onEmailChange,
  disabled = false
}) => {
  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="name">Full Name</Label>
        <Input
          id="name"
          name="name"
          value={name}
          onChange={onNameChange}
          placeholder="Enter full name"
          required
          disabled={disabled}
        />
      </div>
      
      <div className="space-y-2">
        <Label htmlFor="email">Email</Label>
        <Input
          id="email"
          name="email"
          type="email"
          value={email}
          onChange={onEmailChange}
          placeholder="Enter email address"
          required
          disabled={disabled}
        />
      </div>
    </div>
  );
};
