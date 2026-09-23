import React, { useState, useCallback } from 'react';
import { Avatar, AvatarImage, AvatarFallback } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { RefreshCw, Upload } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { features } from '@/config/features';

type AvatarConfig = {
  seed: string;
  customFile: File | null;
  manualMode: boolean;
  skinColor: string;
  hairColor: string;
  topType: string;
  accessoriesType: string;
  facialHairType: string;
  facialHairColor: string;
  eyeType: string;
  eyebrowType: string;
  mouthType: string;
  clotheType: string;
  clotheColor: string;
  graphicType: string;
  noseType: string;
  hatColor: string;
};

type AvatarFeatures = {
  skinColor: string[];
  hairColor: string[];
  topType: string[];
  accessoriesType: string[];
  facialHairType: string[];
  facialHairColor: string[];
  eyeType: string[];
  eyebrowType: string[];
  mouthType: string[];
  clotheType: string[];
  clotheColor: string[];
  graphicType: string[];
  noseType: string[];
  hatColor: string[];
};

interface AvatarEditorProps {
  avatarUrl: string;
  name: string;
  onAvatarChange: (url: string) => void;
  onFileUpload: (file: File) => void;
  disabled?: boolean;
  avatarConfig: AvatarConfig;
  setAvatarConfig: React.Dispatch<React.SetStateAction<AvatarConfig>>;
  avatarFeatures: AvatarFeatures;
  enableAvatarCustomization: boolean;
}

export function AvatarEditor({
  avatarUrl,
  name,
  onAvatarChange,
  onFileUpload,
  avatarConfig,
  setAvatarConfig,
  avatarFeatures,
  enableAvatarCustomization
}: AvatarEditorProps) {
  // Debug effect to log when avatarUrl changes
  React.useEffect(() => {
    console.log('Avatar URL changed:', avatarUrl);
  }, [avatarUrl]);
  
  // State for client-side rendering
  const [isClient, setIsClient] = React.useState(false);
  
  // Set isClient to true after component mounts (client-side only)
  React.useEffect(() => {
    setIsClient(true);
  }, []);
  
  // All hooks must be called before any conditional returns
  const toggleManualMode = useCallback(() => {
    if (enableAvatarCustomization) {
      setAvatarConfig(prev => ({
        ...prev,
        manualMode: !prev.manualMode
      }));
    }
  }, [enableAvatarCustomization, setAvatarConfig]);
  
  const regenerateAvatar = useCallback(() => {
    const newSeed = Math.random().toString(36).substring(7);
    
    if (avatarConfig.manualMode) {
      // In manual mode, keep the current manual settings but update the seed
      setAvatarConfig(prev => ({
        ...prev,
        seed: newSeed
      }));
      
      // Generate new avatar with current manual settings but new seed
      const params = new URLSearchParams();
      params.set('seed', newSeed);
      
      // Add all manual settings to the params
      const configParams = {
        skinColor: avatarConfig.skinColor,
        hairColor: avatarConfig.hairColor,
        topType: avatarConfig.topType,
        accessoriesType: avatarConfig.accessoriesType,
        facialHairType: avatarConfig.facialHairType,
        facialHairColor: avatarConfig.facialHairColor,
        eyeType: avatarConfig.eyeType,
        eyebrowType: avatarConfig.eyebrowType,
        mouthType: avatarConfig.mouthType,
        clotheType: avatarConfig.clotheType,
        clotheColor: avatarConfig.clotheColor,
        graphicType: avatarConfig.graphicType,
        noseType: avatarConfig.noseType,
        hatColor: avatarConfig.hatColor
      };
      
      Object.entries(configParams).forEach(([key, val]) => {
        if (val) {
          const paramKey = key.replace(/([A-Z])/g, (match) => `_${match.toLowerCase()}`);
          const paramValue = typeof val === 'string' ? val.replace('#', '') : val;
          params.set(paramKey, paramValue);
        }
      });
      
      const newAvatar = `https://api.dicebear.com/7.x/avataaars/svg?${params.toString()}`;
      onAvatarChange(newAvatar);
    } else {
      // In random mode, just update the seed and let the API generate a completely random avatar
      setAvatarConfig(prev => ({
        ...prev,
        seed: newSeed,
        manualMode: false
      }));
      
      const newAvatar = `https://api.dicebear.com/7.x/avataaars/svg?seed=${newSeed}`;
      onAvatarChange(newAvatar);
    }
  }, [avatarConfig, onAvatarChange, setAvatarConfig]);
  
  // Early return for server-side rendering
  if (!isClient) {
    return null;
  }

  const generateAvatarUrl = () => {
    if (avatarConfig.customFile) {
      return avatarUrl;
    }
    
    const params = new URLSearchParams();
    const seed = avatarConfig.seed || Math.random().toString(36).substring(7);
    params.set('seed', seed);
    
    if (avatarConfig.manualMode) {
      if (avatarConfig.skinColor) params.set('skinColor', avatarConfig.skinColor.replace('#', ''));
      if (avatarConfig.hairColor) params.set('hairColor', avatarConfig.hairColor.replace('#', ''));
      if (avatarConfig.topType) params.set('topType', avatarConfig.topType);
      // Add other parameters as needed
    }
    
    return `https://api.dicebear.com/7.x/avataaars/svg?${params.toString()}`;
  };

  const handleManualFeatureChange = (feature: string, value: string) => {
    const newConfig = {
      ...avatarConfig,
      [feature]: value,
      manualMode: true
    };
    
    setAvatarConfig(newConfig);
    
    const params = new URLSearchParams();
    const seed = avatarConfig.seed || Math.random().toString(36).substring(7);
    params.set('seed', seed);
    
    const configParams = {
      skinColor: newConfig.skinColor,
      hairColor: newConfig.hairColor,
      topType: newConfig.topType,
      accessoriesType: newConfig.accessoriesType,
      facialHairType: newConfig.facialHairType,
      facialHairColor: newConfig.facialHairColor,
      eyeType: newConfig.eyeType,
      eyebrowType: newConfig.eyebrowType,
      mouthType: newConfig.mouthType,
      clotheType: newConfig.clotheType,
      clotheColor: newConfig.clotheColor,
      graphicType: newConfig.graphicType,
      noseType: newConfig.noseType,
      hatColor: newConfig.hatColor
    };
    
    Object.entries(configParams).forEach(([key, val]) => {
      if (val) {
        const paramKey = key.replace(/([A-Z])/g, (match) => `_${match.toLowerCase()}`);
        const paramValue = typeof val === 'string' ? val.replace('#', '') : val;
        params.set(paramKey, paramValue);
      }
    });
    
    const newAvatar = `https://api.dicebear.com/7.x/avataaars/svg?${params.toString()}`;
    onAvatarChange(newAvatar);
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      onFileUpload(e.target.files[0]);
    }
  };

  const renderColorPicker = (label: string, colors: string[], selectedColor: string, onChange: (color: string) => void) => (
    <div className="space-y-2">
      <Label>{label}</Label>
      <div className="flex flex-wrap gap-2">
        {colors.map(color => (
          <button
            key={color}
            type="button"
            className={`w-6 h-6 rounded-full border-2 ${selectedColor === color ? 'ring-2 ring-offset-2 ring-primary' : 'border-transparent'}`}
            style={{ backgroundColor: color }}
            onClick={() => onChange(color)}
            title={color}
          />
        ))}
      </div>
    </div>
  );

  const renderSelect = (label: string, value: string, options: string[], onChange: (value: string) => void) => (
    <div className="space-y-2">
      <Label>{label}</Label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger>
          <SelectValue placeholder={`Select ${label.toLowerCase()}`} />
        </SelectTrigger>
        <SelectContent className="max-h-60">
          {options.map(option => (
            <SelectItem key={option} value={option}>
              {option === 'Blank' ? 'None' : option.replace(/([A-Z])/g, ' $1').trim()}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );

  // Render simplified UI when customization is disabled
  if (!enableAvatarCustomization) {
    console.log('Rendering AvatarEditor - avatarUrl:', avatarUrl, 'name:', name);
    
    return (
      <div className="space-y-4">
        <Label>User Avatar</Label>
        <div className="flex items-start space-x-4">
          <div className="flex-shrink-0">
            <Avatar className="w-24 h-24 border-2 border-muted">
              <div style={{ display: 'none' }}>Debug: {avatarUrl}</div>
              <AvatarImage 
                src={avatarUrl} 
                onError={(e) => {
                  console.error('Error loading avatar:', e);
                  console.error('Failed URL:', avatarUrl);
                  // If there's an error, force a re-render with a new URL
                  if (avatarUrl && !avatarUrl.includes('?')) {
                    onAvatarChange(`${avatarUrl}?t=${Date.now()}`);
                  } else if (avatarUrl) {
                    onAvatarChange(`${avatarUrl.split('?')[0]}?t=${Date.now()}`);
                  }
                }}
                onLoad={() => console.log('Avatar loaded successfully:', avatarUrl)}
                key={avatarUrl} // Force re-render when URL changes
              />
              <AvatarFallback className="text-2xl">
                {name?.charAt(0) || "U"}
              </AvatarFallback>
            </Avatar>
          </div>
          <div className="relative flex-1 flex flex-col items-center justify-center border-2 border-dashed border-border rounded-lg p-6">
            {avatarConfig.customFile ? (
              <div className="text-center w-full">
                <p className="text-sm text-muted-foreground mb-2 truncate">{avatarConfig.customFile.name}</p>
              </div>
            ) : (
              <div className="text-center">
                <Upload className="h-12 w-12 text-muted-foreground mx-auto mb-2" />
                <p className="text-sm font-medium">Click to upload an image</p>
                <p className="text-xs text-muted-foreground mt-1">PNG, JPG or SVG (max 2MB)</p>
              </div>
            )}
            <input 
              type="file" 
              accept="image/*" 
              onChange={handleFileChange}
              className="absolute inset-0 w-full h-full opacity-0 cursor-pointer z-10"
            />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <Label>User Avatar</Label>
      <Tabs defaultValue="generator">
        <TabsList className="grid grid-cols-2">
          <TabsTrigger value="generator">Generate Avatar</TabsTrigger>
          <TabsTrigger value="upload">Upload Image</TabsTrigger>
        </TabsList>
        
        <TabsContent value="generator" className="space-y-4 mt-4">
          <div className="flex items-start space-x-4">
            <div className="flex-shrink-0">
              <Avatar className="w-24 h-24 border-2 border-muted">
                <AvatarImage src={avatarUrl} />
                <AvatarFallback className="text-2xl">
                  {name?.charAt(0) || "U"}
                </AvatarFallback>
              </Avatar>
            </div>
            <div className="space-y-2 flex-1">
              <div className="flex space-x-2">
                {avatarConfig.manualMode && (
                  <Button 
                    type="button" 
                    variant="default"
                    onClick={regenerateAvatar}
                    className="flex-1"
                  >
                    <RefreshCw size={16} className="mr-2" />
                    Randomize
                  </Button>
                )}
                <Button 
                  type="button" 
                  variant={avatarConfig.manualMode ? "default" : "outline"} 
                  onClick={toggleManualMode}
                  className={`flex-1 ${!avatarConfig.manualMode ? 'ml-auto' : ''}`}
                >
                  {avatarConfig.manualMode ? "Manual Mode" : "Random Mode"}
                </Button>
              </div>
            </div>
          </div>
          
          {avatarConfig.manualMode && (
            <div className="space-y-4 pt-2 max-h-[500px] overflow-y-auto pr-2">
              {renderColorPicker("Skin Color", avatarFeatures.skinColor, avatarConfig.skinColor, 
                (color) => handleManualFeatureChange('skinColor', color))}
              
              {renderSelect("Hair Style", avatarConfig.topType, avatarFeatures.topType,
                (value) => handleManualFeatureChange('topType', value))}
              
              {renderColorPicker("Hair Color", avatarFeatures.hairColor, avatarConfig.hairColor,
                (color) => handleManualFeatureChange('hairColor', color))}
              
              {renderSelect("Eyes", avatarConfig.eyeType, avatarFeatures.eyeType,
                (value) => handleManualFeatureChange('eyeType', value))}
              
              {renderSelect("Eyebrows", avatarConfig.eyebrowType, avatarFeatures.eyebrowType,
                (value) => handleManualFeatureChange('eyebrowType', value))}
              
              {renderSelect("Mouth", avatarConfig.mouthType, avatarFeatures.mouthType,
                (value) => handleManualFeatureChange('mouthType', value))}
              
              {renderSelect("Facial Hair", avatarConfig.facialHairType, avatarFeatures.facialHairType,
                (value) => handleManualFeatureChange('facialHairType', value))}
              
              {avatarConfig.facialHairType !== 'Blank' && 
                renderColorPicker("Facial Hair Color", avatarFeatures.facialHairColor, avatarConfig.facialHairColor,
                  (color) => handleManualFeatureChange('facialHairColor', color))}
              
              {renderSelect("Accessories", avatarConfig.accessoriesType, avatarFeatures.accessoriesType,
                (value) => handleManualFeatureChange('accessoriesType', value))}
              
              {renderSelect("Clothing", avatarConfig.clotheType, avatarFeatures.clotheType,
                (value) => handleManualFeatureChange('clotheType', value))}
              
              {renderColorPicker("Clothing Color", avatarFeatures.clotheColor, avatarConfig.clotheColor,
                (color) => handleManualFeatureChange('clotheColor', color))}
              
              {avatarConfig.clotheType === 'GraphicShirt' && 
                renderSelect("Graphic Design", avatarConfig.graphicType, avatarFeatures.graphicType,
                  (value) => handleManualFeatureChange('graphicType', value))}
            </div>
          )}
        </TabsContent>
        
        <TabsContent value="upload" className="space-y-4 mt-4">
          <div className="relative flex flex-col items-center justify-center border-2 border-dashed border-border rounded-lg p-6">
            {avatarConfig.customFile ? (
              <div className="text-center">
                <Avatar className="w-24 h-24 mx-auto mb-4">
                  <AvatarImage src={avatarUrl} />
                </Avatar>
                <p className="text-sm text-muted-foreground mb-2">{avatarConfig.customFile.name}</p>
              </div>
            ) : (
              <div className="text-center">
                <Upload className="h-12 w-12 text-muted-foreground mx-auto mb-2" />
                <p className="text-sm font-medium">Click to upload an image</p>
                <p className="text-xs text-muted-foreground mt-1">PNG, JPG or SVG (max 2MB)</p>
              </div>
            )}
            <input 
              type="file" 
              accept="image/*" 
              onChange={handleFileChange}
              className="absolute inset-0 w-full h-full opacity-0 cursor-pointer z-10"
            />
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
};
