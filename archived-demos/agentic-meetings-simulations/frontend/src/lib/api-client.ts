import axios, { AxiosInstance, AxiosError } from 'axios';
import { toast } from 'sonner';

export interface APIResponse<T = any> {
  status: 'success' | 'error';
  data?: T;
  message?: string;
  meta?: Record<string, any>;
}

class APIClient {
  private client: AxiosInstance;
  
  constructor(baseURL: string) {
    this.client = axios.create({
      baseURL,
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
    });
    
    // Handle errors globally
    this.client.interceptors.response.use(
      (response) => response,
      (error: AxiosError) => {
        const responseData = error.response?.data as any;
        const message = responseData?.message || responseData?.detail || error.message;
        const status = error.response?.status;
        
        if (status === 401) {
            toast.error("Session Expired", { description: "Please log in again." });
        } else if (status === 403) {
            toast.error("Access Denied", { description: "You don't have permission for this." });
        } else if (status === 404) {
            // Usually not shown for GETs that return null, but for explicit actions
            console.warn("Resource not found:", error.config?.url);
        } else if (status === 500) {
            toast.error("System Fault", { 
                description: "The backend encountered a critical validation error or system failure.",
                duration: 5000
            });
        } else if (error.code === 'ECONNABORTED') {
            toast.warning("Network Latency", { 
                description: "Connection timed out. Retrying request...",
            });
        } else {
            toast.error("Operational Hub Error", { description: message });
        }

        console.error("API Error:", error.response?.data || error.message);
        return Promise.reject(error);
      }
    );
  }
  
  async get<T>(url: string, params?: Record<string, any>): Promise<T> {
    const response = await this.client.get<APIResponse<T>>(url, { params });
    if (response.data.status === 'error') {
       throw new Error(response.data.message || 'API Error');
    }
    return response.data.data as T;
  }
  
  async post<T>(url: string, data?: any): Promise<T> {
    const response = await this.client.post<APIResponse<T>>(url, data);
    return response.data.data as T;
  }

  async put<T>(url: string, data?: any): Promise<T> {
    const response = await this.client.put<APIResponse<T>>(url, data);
    return response.data.data as T;
  }

  async patch<T>(url: string, data?: any): Promise<T> {
    const response = await this.client.patch<APIResponse<T>>(url, data);
    return response.data.data as T;
  }
  
  async delete<T>(url: string): Promise<T> {
    const response = await this.client.delete<APIResponse<T>>(url);
    return response.data.data as T;
  }
}

export const apiClient = new APIClient(process.env.NEXT_PUBLIC_API_URL || '/api/v1');
