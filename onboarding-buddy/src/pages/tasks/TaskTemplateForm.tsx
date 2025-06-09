import React, { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { useTask } from "@/contexts/task";
import { TaskTemplate } from "@/types/tasks";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { toast } from "sonner";
import { ChevronLeft, Plus, X } from "lucide-react";

// Define the form data type that matches the required fields for a new task template
type TaskTemplateFormData = Omit<TaskTemplate, 'id' | 'createdAt' | 'createdBy'>;

// Validation error type
interface FormErrors {
  name?: string;
  description?: string;
  estimatedEffort?: string;
  deliverables?: string;
}

const TaskTemplateForm: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { taskTemplates, addTaskTemplate, updateTaskTemplate } = useTask();
  
  const isEditing = id;
  const templateToEdit = isEditing 
    ? taskTemplates.find(template => template.id === id) 
    : null;
    
  // Check if the current user is a Manager and the template was created by an Admin
  const isReadOnly = isEditing && 
                   templateToEdit && 
                   user?.role === 'manager' && 
                   templateToEdit.createdBy !== user.id;
  
  const [formData, setFormData] = useState<TaskTemplateFormData>({
    name: "",
    description: "",
    deliverables: [""],
    estimatedEffort: 1,
    createdByRole: user?.role || 'manager',
  });
  
  const [errors, setErrors] = useState<FormErrors>({});
  
  // Load template data if editing
  useEffect(() => {
    if (isEditing && templateToEdit) {
      setFormData({
        name: templateToEdit.name,
        description: templateToEdit.description,
        deliverables: [...templateToEdit.deliverables],
        estimatedEffort: templateToEdit.estimatedEffort,
        createdByRole: templateToEdit.createdByRole,
      });
    }
  }, [isEditing, templateToEdit]);

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>
  ) => {
    const { name, value } = e.target;
    
    if (name === "estimatedEffort") {
      const numValue = Number(value);
      if (numValue < 1) return; // Don't allow less than 1 day
      setFormData(prev => ({ ...prev, [name]: numValue }));
    } else {
      setFormData(prev => ({ ...prev, [name]: value }));
    }
  };

  const handleDeliverableChange = (index: number, value: string) => {
    const updatedDeliverables = [...formData.deliverables];
    updatedDeliverables[index] = value;
    setFormData(prev => ({ ...prev, deliverables: updatedDeliverables }));
  };

  const addDeliverable = () => {
    setFormData(prev => ({
      ...prev,
      deliverables: [...prev.deliverables, ""]
    }));
  };

  const removeDeliverable = (index: number) => {
    if (formData.deliverables.length <= 1) {
      return; // Keep at least one deliverable
    }
    
    const updatedDeliverables = formData.deliverables.filter((_, i) => i !== index);
    setFormData(prev => ({ ...prev, deliverables: updatedDeliverables }));
  };

  // Validate form fields
  const validateForm = (): boolean => {
    const newErrors: FormErrors = {};
    let isValid = true;

    if (!formData.name.trim()) {
      newErrors.name = 'Template name is required';
      isValid = false;
    }

    if (!formData.description.trim()) {
      newErrors.description = 'Description is required';
      isValid = false;
    }

    if (formData.estimatedEffort < 1) {
      newErrors.estimatedEffort = 'Estimated effort must be at least 1 day';
      isValid = false;
    }

    const filteredDeliverables = formData.deliverables.filter(d => d.trim() !== '');
    if (filteredDeliverables.length === 0) {
      newErrors.deliverables = 'At least one deliverable is required';
      isValid = false;
    }

    setErrors(newErrors);
    return isValid;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    if (!user) {
      toast.error("You must be logged in to perform this action");
      return;
    }
    
    if (!validateForm()) {
      return; // Don't proceed if validation fails
    }
    
    // Filter out empty deliverables
    const filteredDeliverables = formData.deliverables.filter(d => d.trim() !== '');
    
    try {
      if (isEditing && id) {
        await updateTaskTemplate(id, {
          ...formData,
          deliverables: filteredDeliverables,
          estimatedEffort: formData.estimatedEffort,
          name: formData.name,
          description: formData.description
        });
        toast.success("Task template updated successfully");
      } else {
        await addTaskTemplate(
          {
            name: formData.name,
            description: formData.description,
            deliverables: filteredDeliverables,
            estimatedEffort: formData.estimatedEffort,
            createdBy: user.id,
          },
          user.id,
          user.role as 'admin' | 'manager' | 'mentor' | 'new-hire'
        );
        toast.success("Task template created successfully");
      }
      navigate("/task-templates");
    } catch (error) {
      console.error("Error saving task template:", error);
      toast.error("An error occurred while saving the task template");
    }
  };

  return (
    <div className="container max-w-2xl mx-auto">
      <Button 
        variant="outline" 
        className="mb-6"
        onClick={() => navigate("/task-templates")}
      >
        <ChevronLeft size={16} className="mr-1" />
        Back to Task Templates
      </Button>
      
      <Card>
        <CardHeader>
          <CardTitle>
            {isReadOnly 
              ? "View Task Template" 
              : isEditing 
                ? "Edit Task Template" 
                : "Create Task Template"
            }
          </CardTitle>
        </CardHeader>
        <form onSubmit={handleSubmit}>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="name">Template Name</Label>
              <Input
                id="name"
                name="name"
                value={formData.name}
                onChange={handleChange}
                placeholder="E.g., Company Introduction"
                className={errors.name ? 'border-red-500' : ''}
                readOnly={isReadOnly}
              />
              {errors.name && <p className="text-sm text-red-500 mt-1">{errors.name}</p>}
            </div>
            
            <div className="space-y-2">
              <Label htmlFor="description">Description</Label>
              <Textarea
                id="description"
                name="description"
                value={formData.description}
                onChange={handleChange}
                placeholder="Describe what this task involves"
                rows={4}
                className={errors.description ? 'border-red-500' : ''}
                readOnly={isReadOnly}
              />
              {errors.description && <p className="text-sm text-red-500 mt-1">{errors.description}</p>}
            </div>
            
            <div className="space-y-2">
              <Label htmlFor="estimatedEffort">Estimated Effort (days)</Label>
              <Input
                id="estimatedEffort"
                name="estimatedEffort"
                type="number"
                min={1}
                value={formData.estimatedEffort}
                onChange={handleChange}
                className={errors.estimatedEffort ? 'border-red-500' : ''}
                readOnly={isReadOnly}
              />
              {errors.estimatedEffort && <p className="text-sm text-red-500 mt-1">{errors.estimatedEffort}</p>}
              <p className="text-xs text-muted-foreground">
                How many days do you expect this task to take?
              </p>
            </div>
            
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>Deliverables</Label>
                {errors.deliverables && <p className="text-sm text-red-500">{errors.deliverables}</p>}
              </div>
              <p className="text-xs text-muted-foreground mb-2">
                List the specific deliverables that the new hire should complete
              </p>
              
              {formData.deliverables.map((deliverable, index) => (
                <div key={index} className="flex gap-2 mb-2">
                  <Input
                    value={deliverable}
                    onChange={(e) => {
                      if (!isReadOnly) {
                        handleDeliverableChange(index, e.target.value);
                        // Clear deliverable error when user types
                        if (errors.deliverables) {
                          setErrors(prev => ({ ...prev, deliverables: undefined }));
                        }
                      }
                    }}
                    placeholder={`Deliverable ${index + 1}`}
                    className={`${errors.deliverables ? 'border-red-500' : ''} ${isReadOnly ? 'bg-muted' : ''}`}
                    readOnly={isReadOnly}
                  />
                  {!isReadOnly && (
                    <Button
                      type="button"
                      variant="outline"
                      size="icon"
                      onClick={() => removeDeliverable(index)}
                      disabled={formData.deliverables.length <= 1}
                    >
                      <X size={16} />
                    </Button>
                  )}
                </div>
              ))}
              
              {!isReadOnly && (
                <Button
                  type="button"
                  variant="outline"
                  className="w-full"
                  onClick={addDeliverable}
                >
                  <Plus size={16} className="mr-1" />
                  Add Another Deliverable
                </Button>
              )}
            </div>
          </CardContent>
          <CardFooter className="flex justify-between">
            <Button 
              type="button" 
              variant="outline"
              onClick={() => navigate("/task-templates")}
            >
              {isReadOnly ? 'Back' : 'Cancel'}
            </Button>
            {!isReadOnly && (
              <Button type="submit">
                {isEditing ? "Update Template" : "Create Template"}
              </Button>
            )}
          </CardFooter>
        </form>
      </Card>
    </div>
  );
};

export default TaskTemplateForm;
