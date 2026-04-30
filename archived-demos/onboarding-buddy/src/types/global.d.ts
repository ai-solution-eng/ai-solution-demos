// Extend the Window interface to include our custom env object
declare global {
  interface Window {
    env: {
      VITE_AI_ENDPOINT?: string;
      VITE_AI_API_KEY?: string;
      VITE_AI_MODEL?: string;
      [key: string]: any; // Allow any other properties
    };
  }
}

export {}; // This file needs to be a module
