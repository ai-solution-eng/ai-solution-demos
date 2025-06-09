
import React, { createContext, useContext, useState, useEffect } from "react";
import { toast } from "sonner";

// Define the shape of the config file
interface ConfigFileAI {
  endpoint?: string;
  model?: string;
  apiKey?: string;
}

interface ConfigFile {
  ai?: ConfigFileAI;
}

export type APIType = 'openai' | 'gemini';

export interface AIConfigType {
  // Unified configuration
  endpoint: string;
  model: string;
  apiKey: string;
  temperature: number;
  maxTokens: number;
  skipVerifySSL: boolean; // Skip SSL certificate verification
  onStreamChunk?: (chunk: string) => void; // Optional callback for streaming
}

interface AIConfigContextType {
  aiConfig: AIConfigType;
  updateAIConfig: (config: Partial<AIConfigType>) => void;
}

const defaultAIConfig: AIConfigType = {
  endpoint: 'https://generativelanguage.googleapis.com/v1beta/models',
  model: 'gemini-2.0-flash',
  apiKey: '',
  temperature: 0.7,
  maxTokens: 1000,
  skipVerifySSL: false, // Default to false for security
};

// Load configuration from config.json
const loadConfig = async (): Promise<AIConfigType> => {
  try {
    // Try to load config from the config file
    let configFromFile: ConfigFile = { ai: {} };
    try {
      const response = await fetch('/config/config.json');
      if (response.ok) {
        const contentType = response.headers.get('content-type');
        if (contentType && contentType.includes('application/json')) {
          configFromFile = (await response.json()) as ConfigFile;
        } else {
          console.warn('Received non-JSON response from config file. Using default configuration.');
        }
      } else {
        console.warn(`Failed to load config file (status: ${response.status}). Using default configuration.`);
      }
    } catch (fetchError) {
      console.warn('Error fetching config file:', fetchError);
      // Continue with default config
    }
    
    // Get saved config from localStorage if it exists
    let savedConfig: Partial<AIConfigType> = {};
    const savedConfigString = localStorage.getItem("aiConfig");
    if (savedConfigString) {
      try {
        savedConfig = JSON.parse(savedConfigString);
      } catch (error) {
        console.error('Failed to parse aiConfig from localStorage. Clearing corrupted data:', error);
        localStorage.removeItem("aiConfig"); // Clear corrupted data
        // savedConfig remains {} as initialized
      }
    }
    
    // Merge configurations with fallbacks
    const config = {
      // Start with defaults
      ...defaultAIConfig,
      
      // Apply config from file if available
      ...(configFromFile.ai ? {
        endpoint: configFromFile.ai.endpoint || defaultAIConfig.endpoint,
        model: configFromFile.ai.model || defaultAIConfig.model,
        apiKey: configFromFile.ai.apiKey || '',
      } : {}),
      
      // Apply saved config (overrides file config)
      ...savedConfig,
    };
    
    return config;
  } catch (error) {
    console.error('Unexpected error in loadConfig:', error);
    return defaultAIConfig;
  }
};

const AIConfigContext = createContext<AIConfigContextType | undefined>(undefined);

export const AIConfigProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [aiConfig, setAIConfig] = useState<AIConfigType>(defaultAIConfig);
  const [isLoading, setIsLoading] = useState(true);

  // Update the proxy endpoint when the config changes
  const updateProxyEndpoint = React.useCallback(async (endpoint: string) => {
    if (!endpoint) return;
    
    try {
      const proxyBaseUrl = window.location.origin;
      const proxyUrl = new URL(proxyBaseUrl);
      const proxyEndpoint = new URL('/api/llm-endpoint', proxyUrl);
      
      console.log('Updating proxy endpoint to:', endpoint);
      const response = await fetch(proxyEndpoint.toString(), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        body: JSON.stringify({ endpoint }),
      });
      
      const responseData = await response.json().catch(() => ({}));
      
      if (!response.ok) {
        console.error('Failed to update proxy endpoint:', response.status, response.statusText, responseData);
        throw new Error(responseData.error || 'Failed to update proxy endpoint');
      }
      
      console.log('Proxy endpoint updated successfully:', responseData);
      toast.success('LLM endpoint updated successfully');
      return responseData;
    } catch (error) {
      console.error('Error updating proxy endpoint:', error);
      const errorMessage = error instanceof Error ? error.message : 'Failed to update LLM endpoint';
      toast.error(`Error: ${errorMessage}`);
      throw error;
    }
  }, []);

  const updateAIConfig = React.useCallback(async (updates: Partial<AIConfigType>) => {
    setAIConfig(prev => {
      const newConfig = { ...prev, ...updates };
      // Save to localStorage
      localStorage.setItem("aiConfig", JSON.stringify(newConfig));
      return newConfig;
    });

    // If the endpoint changed, update the proxy
    if (updates.endpoint && updates.endpoint !== aiConfig.endpoint) {
      try {
        await updateProxyEndpoint(updates.endpoint);
      } catch (error) {
        console.error('Failed to update proxy endpoint:', error);
      }
    }
  }, [aiConfig.endpoint, updateProxyEndpoint]);

  // Load config on component mount and update proxy
  useEffect(() => {
    let isMounted = true;
    let retryCount = 0;
    const MAX_RETRIES = 5;
    const RETRY_DELAY = 1000; // 1 second

    const updateProxyWithRetry = async (endpoint: string, attempt: number): Promise<boolean> => {
      try {
        console.log(`[Proxy] Attempting to update proxy endpoint (attempt ${attempt + 1}/${MAX_RETRIES})`);
        await updateProxyEndpoint(endpoint);
        console.log('[Proxy] Successfully updated proxy endpoint');
        return true;
      } catch (error) {
        if (attempt < MAX_RETRIES - 1) {
          console.warn(`[Proxy] Failed to update proxy, retrying in ${RETRY_DELAY}ms...`, error);
          await new Promise(resolve => setTimeout(resolve, RETRY_DELAY));
          return updateProxyWithRetry(endpoint, attempt + 1);
        }
        console.error('[Proxy] Max retries reached, could not update proxy endpoint', error);
        toast.error('Failed to connect to proxy server. Please refresh the page to retry.');
        return false;
      }
    };

    const loadInitialConfig = async () => {
      try {
        const config = await loadConfig();
        if (!isMounted) return;
        
        setAIConfig(config);
        
        // Initialize proxy with the loaded endpoint if one exists
        if (config.endpoint) {
          console.log('[Config] Initializing proxy with endpoint:', config.endpoint);
          await updateProxyWithRetry(config.endpoint, 0);
        } else {
          console.log('[Config] No endpoint configured, proxy will wait for manual configuration');
        }
      } catch (error) {
        console.error('[Config] Error initializing configuration:', error);
        toast.error('Failed to load configuration');
      } finally {
        if (isMounted) {
          setIsLoading(false);
        }
      }
    };
    
    loadInitialConfig();
    
    return () => {
      isMounted = false;
    };
  }, [updateProxyEndpoint]);

  if (isLoading) {
    return <div>Loading configuration...</div>;
  }

  return (
    <AIConfigContext.Provider value={{ aiConfig, updateAIConfig }}>
      {children}
    </AIConfigContext.Provider>
  );
};

export const useAIConfig = () => {
  const context = useContext(AIConfigContext);
  if (context === undefined) {
    throw new Error("useAIConfig must be used within an AIConfigProvider");
  }
  return context;
};
