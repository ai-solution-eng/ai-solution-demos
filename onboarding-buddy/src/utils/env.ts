/**
 * Get environment variable value
 * @param key Environment variable key
 * @returns Value of the environment variable or empty string if not found
 */
export const getEnv = (key: keyof Window['env']): string => {
  if (typeof window !== 'undefined') {
    return window.env?.[key] || '';
  }
  return '';
};

/**
 * Application configuration with environment variables
 */
export const config = {
  ai: {
    endpoint: getEnv('VITE_AI_ENDPOINT'),
    apiKey: getEnv('VITE_AI_API_KEY'),
    model: getEnv('VITE_AI_MODEL'),
  },
};
