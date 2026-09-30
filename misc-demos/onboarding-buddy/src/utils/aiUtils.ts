import { AIConfigType } from "../contexts/AIConfigContext";
import { aiPrompts } from "../config/features";
import { PersonalizedTask } from "../types/tasks";

export interface AIMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

/**
 * Calls the configured LLM with the given messages and returns the response.
 * 
 * @param {AIMessage[]} messages - Array of message objects with role and content.
 * @param {Object} config - Configuration object.
 * @param {string} config.endpoint - The API endpoint to call.
 * @param {string} config.model - The model to use for the completion.
 * @param {string} config.apiKey - The API key for authentication.
 * @param {number} [config.temperature] - Sampling temperature to use, between 0 and 2.
 * @param {number} [config.maxTokens] - The maximum number of tokens to generate in the completion.
 * 
 * @returns {Promise<string>} The content of the response message from the LLM.
 * 
 * @throws Will throw an error if the API call fails or if the response is not successful.
 */
export async function callLLM(
  messages: AIMessage[],
  config: {
    endpoint: string;
    model: string;
    apiKey: string;
    temperature?: number;
    maxTokens?: number;
    skipVerifySSL?: boolean;
    onStreamChunk?: (chunk: string) => void;
    stream?: boolean;
  }
): Promise<string> {
  try {
    const temperature = config.temperature ?? 0.7;
    const maxTokens = config.maxTokens ?? 1000;

    // Use relative URL to let the proxy handle the request
    const requestUrl = '/api/chat';
    
    console.log(`[LLM] Using proxy URL: ${requestUrl}`);
    console.log(`[LLM] Target endpoint: ${config.endpoint}`);

    // Prepare the request headers
    const headers: HeadersInit = {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
      'Authorization': `Bearer ${config.apiKey}`,
    };

    // Prepare the request body
    const stream = config.stream ?? false; // Default to false if not specified
    const requestBody = {
      model: config.model,
      messages: messages,
      temperature: temperature,
      max_tokens: maxTokens,
      stream: stream
    };
    
    console.log(`[LLM] Using streaming: ${stream}`);

    console.log(`[LLM] Sending request to proxy: ${requestUrl}`);

    try {
      // Log the request for debugging
      console.log(`[LLM] Sending POST request to: ${requestUrl}`);

      // Make the request
      const response = await fetch(requestUrl, {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(requestBody),
        credentials: 'omit', // Don't send cookies
      });

      // Log the response for debugging
      console.log(`[LLM] Response status: ${response.status} ${response.statusText}`);
      console.log('[LLM] Response headers:', Object.fromEntries(response.headers.entries()));

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        console.error('[LLM] Error response:', errorData);
        throw new Error(`HTTP error! status: ${response.status} ${JSON.stringify(errorData)}`);
      }

      // Log response headers for debugging
      console.log('[LLM] Response headers:', Object.fromEntries(response.headers.entries()));
      
      // Handle streaming response
      if (stream && config.onStreamChunk) {
        console.log('[LLM] Handling streaming response');
        
        // Check if response is using SSE (text/event-stream)
        const contentType = response.headers.get('content-type') || '';
        console.log('[LLM] Content-Type:', contentType);
        
        if (contentType.includes('text/event-stream')) {
          console.log('[LLM] Detected SSE stream');
          const reader = response.body?.getReader();
          const decoder = new TextDecoder('utf-8');
          let buffer = '';
          let fullResponse = '';
          let lastProcessedContent = ''; // Track the last processed content chunk

          if (!reader) {
            throw new Error('Failed to get reader from response body');
          }

          try {
            // eslint-disable-next-line no-constant-condition
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              
              try {
                // Decode the chunk and add to buffer
                const chunk = decoder.decode(value, { stream: true });
                console.log('[LLM] Raw chunk received:', JSON.stringify(chunk));
                buffer += chunk;
                
                // Process complete SSE messages (separated by double newlines)
                const events = buffer.split('\n\n');
                buffer = events.pop() || ''; // Keep incomplete message in buffer
                
                // Process each complete event
                for (const event of events) {
                  // Skip empty events
                  const trimmedEvent = event.trim();
                  if (!trimmedEvent) continue;
                  
                  // Handle [DONE] event
                  if (trimmedEvent === 'data: [DONE]') {
                    console.log('[LLM] Stream completed');
                    continue;
                  }
                  
                  // Extract and parse the data payload
                  if (trimmedEvent.startsWith('data: ')) {
                    const data = trimmedEvent.slice(6).trim(); // Remove 'data: ' prefix
                    
                    try {
                      const parsed = JSON.parse(data);
                      
                      // Handle both OpenAI and Ollama response formats
                      const contentChunk = parsed.choices?.[0]?.delta?.content || 
                                         parsed.choices?.[0]?.text || 
                                         parsed.response || '';
                      
                      if (contentChunk && contentChunk !== lastProcessedContent) {
                        fullResponse += contentChunk;
                        config.onStreamChunk(contentChunk);
                        lastProcessedContent = contentChunk; // Update the last processed content
                      }
                    } catch (e) {
                      console.error('[LLM] Error parsing event data:', e, 'Event:', trimmedEvent);
                    }
                  }
                }
              } catch (e) {
                console.error('[LLM] Error processing SSE chunk:', e);
                throw e;
              }
            }
            
            return fullResponse;
          } finally {
            reader.releaseLock();
          }
        } else {
          console.warn('[LLM] Streaming requested but response is not SSE, falling back to regular response');
        }
      }

      // Handle non-streaming response
      const responseData = await response.json();
      
      // Extract the content from the response
      const content = responseData.choices?.[0]?.message?.content || responseData.choices?.[0]?.text;
      if (!content) {
        console.error('[LLM] No content in response:', responseData);
        throw new Error('No content in response');
      }

      return content;
    } catch (error) {
      console.error('[LLM] Error in callLLM:', error);
      throw new Error(`Failed to call LLM: ${error.message}`);
    }
  } catch (error) {
    console.error('Error calling LLM:', error);
    throw error;
  }
}

/**
 * Generates a personalized task description based on a task template and parameters.
 */
export async function generatePersonalizedTask(
  taskTemplate: { name: string; description: string; deliverables: string[] },
  difficulty: number,
  priority: number,
  aiConfig: AIConfigType,
  estimatedEffortDays: number
): Promise<string> {
  if (!aiConfig.apiKey) {
    throw new Error('API key not configured');
  }

  const messages: AIMessage[] = [
    { 
      role: 'system', 
      content: aiPrompts.personalizedTask.system 
    },
    { 
      role: 'user', 
      content: aiPrompts.personalizedTask.user(
        taskTemplate, 
        difficulty, 
        priority, 
        estimatedEffortDays
      )
    }
  ];

  return callLLM(messages, {
    endpoint: aiConfig.endpoint,
    model: aiConfig.model,
    apiKey: aiConfig.apiKey,
    temperature: aiConfig.temperature,
    maxTokens: aiConfig.maxTokens,
    stream: false // Disable streaming for task generation
  });
}

/**
 * Generates a response from the AI assistant based on the conversation history and task context.
 */
export async function generateAssistantResponse(
  messages: Array<{role: 'user' | 'assistant', content: string}>,
  task: PersonalizedTask,
  aiConfig: AIConfigType
): Promise<string> {
  if (!aiConfig.apiKey) {
    throw new Error('API key not configured');
  }

  // Use only the task context as the system message
  const systemMessage: AIMessage = { 
    role: 'system', 
    content: aiPrompts.assistant.context({
      name: task.name,
      description: task.description,
      expandedDescription: task.expandedDescription,
      deliverables: task.deliverables,
      estimatedEffort: task.estimatedEffort
    })
  };
  
  const conversationMessages: AIMessage[] = messages.map(msg => ({
    role: msg.role === 'assistant' ? 'assistant' : 'user',
    content: msg.content
  }));
  
  const conversationHistory: AIMessage[] = [
    systemMessage,
    ...conversationMessages
  ];

  return callLLM(conversationHistory, {
    endpoint: aiConfig.endpoint,
    model: aiConfig.model,
    apiKey: aiConfig.apiKey,
    temperature: aiConfig.temperature, // Use from aiConfig
    maxTokens: aiConfig.maxTokens,   // Use from aiConfig
    onStreamChunk: aiConfig.onStreamChunk, // Pass the callback
    stream: true // Enable streaming for AI Assistant
  });
}
