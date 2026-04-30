import React, { useState } from "react";
import { useAIConfig } from "@/contexts/AIConfigContext";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, CardFooter } from "@/components/ui/card";
import { Slider } from "@/components/ui/slider";
import { Eye, EyeOff, Info } from "lucide-react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface AIMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

const AIConfiguration: React.FC = () => {
  const { aiConfig, updateAIConfig } = useAIConfig();
  
  const [showApiKey, setShowApiKey] = useState(false);
  const [config, setConfig] = useState({
    endpoint: aiConfig.endpoint || 'https://generativelanguage.googleapis.com/v1beta/models',
    model: aiConfig.model || 'gemini-2.0-flash',
    apiKey: aiConfig.apiKey || '',
    temperature: aiConfig.temperature ?? 0.7,
    maxTokens: aiConfig.maxTokens ?? 1000,
    skipVerifySSL: aiConfig.skipVerifySSL ?? false,
  });

  const getEndpointPlaceholder = () => {
    return 'https://my-service.mydomain.com/v1';
  };

  const getModelPlaceholder = () => {
    return 'gemini-2.0-flash';
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value, type, checked } = e.target;
    setConfig(prev => ({
      ...prev,
      [name]: type === 'checkbox' ? checked : value
    }));
  };

  const handleNumberChange = (name: string, value: number) => {
    setConfig(prev => ({
      ...prev,
      [name]: value
    }));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      // Ensure skipVerifySSL is a boolean
      const configToSave = {
        ...config,
        skipVerifySSL: Boolean(config.skipVerifySSL)
      };
      await updateAIConfig(configToSave);
      // Dynamically update backend proxy endpoint
      try {
        await fetch('/api/llm-endpoint', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: configToSave.endpoint })
        });
      } catch (err) {
        console.warn('Failed to update backend proxy endpoint:', err);
      }
      toast.success("AI configuration saved successfully!");
    } catch (error) {
      console.error("Error saving AI config:", error);
      toast.error("Failed to save AI configuration");
    }
  };

  return (
    <div className="container mx-auto py-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold">AI Configuration</h1>
        <p className="text-muted-foreground">
          Configure your AI provider settings for the onboarding assistant.
        </p>
      </div>

      <form onSubmit={handleSubmit}>
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>AI Provider Configuration</CardTitle>
              <CardDescription>
                Configure the connection to your AI provider.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label htmlFor="endpoint">API Endpoint</Label>
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Info className="h-4 w-4 text-muted-foreground" />
                      </TooltipTrigger>
                      <TooltipContent>
                        <p>Base URL for the API endpoint (e.g., https://my-service.mydomain.com/v1)</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
                <Input
                  id="endpoint"
                  name="endpoint"
                  value={config.endpoint}
                  onChange={handleChange}
                  placeholder={getEndpointPlaceholder()}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="model">Model</Label>
                <Input
                  id="model"
                  name="model"
                  value={config.model}
                  onChange={handleChange}
                  placeholder={getModelPlaceholder()}
                />
                <p className="text-xs text-muted-foreground">
                  Model name (e.g., {getModelPlaceholder()})
                </p>
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label htmlFor="apiKey">API Key</Label>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-8 px-2"
                    onClick={() => setShowApiKey(!showApiKey)}
                  >
                    {showApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </Button>
                </div>
                <Input
                  id="apiKey"
                  name="apiKey"
                  type={showApiKey ? "text" : "password"}
                  value={config.apiKey}
                  onChange={handleChange}
                  placeholder="Enter your API key"
                />
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label htmlFor="temperature">Temperature: {config.temperature}</Label>
                </div>
                <Slider
                  id="temperature"
                  min={0}
                  max={1}
                  step={0.1}
                  value={[config.temperature]}
                  onValueChange={(vals) => handleNumberChange('temperature', vals[0])}
                  className="py-4"
                />
                <div className="flex justify-between text-xs text-muted-foreground">
                  <span>Precise</span>
                  <span>Balanced</span>
                  <span>Creative</span>
                </div>
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label htmlFor="maxTokens">Max Tokens: {config.maxTokens}</Label>
                </div>
                <Slider
                  id="maxTokens"
                  min={100}
                  max={4000}
                  step={100}
                  value={[config.maxTokens]}
                  onValueChange={(vals) => handleNumberChange('maxTokens', vals[0])}
                  className="py-4"
                />
              </div>

              <div className="space-y-2 pt-2">
                <div className="flex items-center justify-between">
                  <div className="flex items-center space-x-2">
                    <input
                      type="checkbox"
                      id="skipVerifySSL"
                      name="skipVerifySSL"
                      checked={config.skipVerifySSL}
                      onChange={handleChange}
                      className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                    />
                    <Label htmlFor="skipVerifySSL" className="flex items-center text-sm font-medium text-gray-700">
                      Skip SSL Verification
                    </Label>
                    <TooltipProvider>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Info className="h-4 w-4 text-muted-foreground" />
                        </TooltipTrigger>
                        <TooltipContent className="max-w-xs">
                          <p>Enable this to bypass SSL certificate validation. Only use this for development or internal networks with self-signed certificates.</p>
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                  </div>
                </div>
                <p className="text-xs text-muted-foreground ml-6">For self-signed certificates in development/internal networks</p>
              </div>
            </CardContent>
            <CardFooter className="flex justify-end border-t px-6 py-4">
              <Button type="submit">Save Configuration</Button>
            </CardFooter>
          </Card>
        </div>
      </form>
    </div>
  );
};

export default AIConfiguration;
