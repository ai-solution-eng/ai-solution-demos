// Environment variable types
declare namespace NodeJS {
  interface ProcessEnv {
    readonly VITE_AI_ENDPOINT: string;
    readonly VITE_AI_API_KEY: string;
    readonly VITE_AI_MODEL: string;
  }
}

// Window interface extension for environment variables
declare interface Window {
  env: {
    VITE_AI_ENDPOINT: string;
    VITE_AI_API_KEY: string;
    VITE_AI_MODEL: string;
  };
}
