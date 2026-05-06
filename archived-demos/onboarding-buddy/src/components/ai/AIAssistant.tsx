
import React, { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Send, AlertCircle, Minus, Maximize2, X, MessageSquare } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { useAIConfig } from "@/contexts/AIConfigContext";
import { PersonalizedTask } from "@/types/tasks";
import { generateAssistantResponse } from "@/utils/aiUtils";
import { toast } from "sonner";
import { Alert, AlertDescription } from "@/components/ui/alert";

interface AIAssistantProps {
  task: PersonalizedTask;
}

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

const AIAssistant: React.FC<AIAssistantProps> = ({ task }) => {
  const { user } = useAuth();
  const { aiConfig } = useAIConfig();
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([
    {
      id: "1",
      role: "assistant",
      content: `Hi ${user?.name || "there"}! I'm your AI onboarding assistant. I'm here to help you with your "${task.name}" task. What questions do you have?`,
      timestamp: new Date(),
    },
  ]);
  const [isLoading, setIsLoading] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isMinimized, setIsMinimized] = useState(false);
  const [isHovered, setIsHovered] = useState(false);
  const [isOpen, setIsOpen] = useState(true);

  // Helper function to generate unique IDs
  const generateUniqueId = (() => {
    let counter = 0;
    return () => {
      const timestamp = Date.now().toString(36);
      const random = Math.random().toString(36).substring(2, 6);
      const count = (counter++).toString(36);
      return `${timestamp}-${random}-${count}`;
    };
  })();

  // Separate function to process AI response
  const processAIResponse = useCallback(async (currentMessages: Message[], assistantMessageId: string) => {
    try {
      // Get the conversation history without the placeholder message and exclude the initial assistant message
      const conversationHistory = currentMessages
        .filter(msg => msg.id !== assistantMessageId && !(msg.role === 'assistant' && msg.id === '1'))
        .map(({ role, content }) => ({ role, content }));

      // Only proceed if we have at least one user message
      if (conversationHistory.some(msg => msg.role === 'user')) {
        await generateAssistantResponse(
          conversationHistory,
          task,
          {
            ...aiConfig,
            onStreamChunk: (chunk) => {
              setMessages((prevMessages) =>
                prevMessages.map((msg) =>
                  msg.id === assistantMessageId
                    ? { ...msg, content: msg.content + chunk }
                    : msg
                )
              );
            },
          }
        );
      }
    } catch (err) {
      console.error('Error generating AI response:', err);
      setError('Failed to get a response from the AI assistant. Please try again later.');
      
      // Add error message to chat
      const errorMessage: Message = {
        id: generateUniqueId(),
        role: "assistant",
        content: "I'm sorry, I encountered an error while processing your request. Please try again in a moment.",
        timestamp: new Date(),
      };
      
      setMessages(prev => [...prev, errorMessage]);
    } finally {
      setIsLoading(false);
      setIsSubmitting(false);
    }
  }, [task, aiConfig]);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    
    if (!input.trim() || !aiConfig.apiKey || isSubmitting) return;
    
    setIsSubmitting(true);
    
    // Add user message
    const userMessage: Message = {
      id: generateUniqueId(),
      role: "user",
      content: input,
      timestamp: new Date(),
    };
    
    setInput("");
    setIsLoading(true);
    setError(null);
    
    // Use functional update to get the latest messages
    setMessages(prev => {
      const newMessages = [...prev, userMessage];
      
      // Add a placeholder for the assistant's message
      const assistantMessageId = generateUniqueId();
      const placeholderAIMessage: Message = {
        id: assistantMessageId,
        role: "assistant",
        content: "",
        timestamp: new Date(),
      };
      
      // Process the AI response with the updated messages
      processAIResponse([...newMessages, placeholderAIMessage], assistantMessageId);
      
      return [...newMessages, placeholderAIMessage];
    });
  }, [input, aiConfig.apiKey, isSubmitting, processAIResponse]);

  // Show error alert if API key is not configured
  if (!aiConfig.apiKey) {
    return (
      <Card className="h-full flex flex-col">
        <CardHeader className="bg-accent/30">
          <CardTitle className="text-lg">AI Onboarding Assistant</CardTitle>
        </CardHeader>
        <CardContent className="flex-grow flex items-center justify-center p-6">
          <Alert variant="destructive" className="w-full">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>
              AI Assistant is not properly configured. Please check your AI configuration settings.
            </AlertDescription>
          </Alert>
        </CardContent>
      </Card>
    );
  }

  // Toggle between minimized and expanded states
  const toggleMinimize = () => {
    setIsMinimized(!isMinimized);
  };

  // Toggle widget visibility
  const toggleWidget = (e?: React.MouseEvent) => {
    e?.stopPropagation();
    if (isOpen) {
      setIsOpen(false);
      setIsMinimized(false);
    } else {
      setIsOpen(true);
    }
  };

  // Close the widget
  const handleClose = (e: React.MouseEvent) => {
    e.stopPropagation();
    toggleWidget(e);
  };

  if (!isOpen) {
    return (
      <button
        onClick={toggleWidget}
        className="fixed right-4 bottom-4 z-50 w-14 h-14 rounded-full bg-primary text-primary-foreground flex items-center justify-center shadow-lg hover:bg-primary/90 transition-colors"
        aria-label="Open AI Assistant"
      >
        <MessageSquare size={24} />
      </button>
    );
  }

  return (
    <div 
      className={`fixed right-4 bottom-4 w-[400px] z-50 transition-all duration-200 ${isMinimized ? 'h-[60px]' : 'h-[500px]'}`}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      <Card className="flex flex-col h-full shadow-lg overflow-hidden">
        <CardHeader 
          className="bg-accent/30 p-3 cursor-pointer flex flex-row justify-between items-center"
          onClick={toggleMinimize}
        >
          <CardTitle className="text-lg">AI Onboarding Assistant</CardTitle>
          <div className="flex items-center space-x-2">
            <button 
              onClick={(e) => {
                e.stopPropagation();
                toggleMinimize();
              }} 
              className="p-1 rounded-full hover:bg-accent/50"
              aria-label={isMinimized ? 'Maximize' : 'Minimize'}
            >
              {isMinimized ? <Maximize2 size={16} /> : <Minus size={16} />}
            </button>
            <button 
              onClick={handleClose}
              className="p-1 rounded-full hover:bg-accent/50"
              aria-label="Close"
            >
              <X size={16} />
            </button>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col p-0 h-[calc(100%-57px)]">
          <div className={`flex-1 overflow-y-auto p-4 space-y-4 transition-opacity duration-200 ${isMinimized ? 'opacity-0 h-0 overflow-hidden' : 'opacity-100'}`}>
            {isMinimized ? (
              <div className="flex items-center justify-center h-full text-muted-foreground">
                Click to expand the chat
              </div>
            ) : (
              <>
                {messages.map((message) => (
                  <div
                    key={message.id}
                    className={`flex ${
                      message.role === "user" ? "justify-end" : "justify-start"
                    }`}
                  >
                    <div
                      className={`rounded-lg px-4 py-2 max-w-[80%] ${
                        message.role === "user"
                          ? "bg-primary text-primary-foreground"
                          : "bg-muted"
                      }`}
                    >
                      <p className="whitespace-pre-wrap">{message.content}</p>
                      <p className="text-xs opacity-70 text-right mt-1">
                        {message.timestamp.toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </p>
                    </div>
                  </div>
                ))}
                
                {isLoading && (
                  <div className="flex justify-start">
                    <div className="rounded-lg px-4 py-3 max-w-[80%] bg-muted">
                      <div className="flex space-x-2">
                        <div className="w-2 h-2 rounded-full bg-muted-foreground/30 animate-pulse"></div>
                        <div className="w-2 h-2 rounded-full bg-muted-foreground/30 animate-pulse" style={{ animationDelay: "0.2s" }}></div>
                        <div className="w-2 h-2 rounded-full bg-muted-foreground/30 animate-pulse" style={{ animationDelay: "0.4s" }}></div>
                      </div>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
          
          {!isMinimized && error && (
            <div className="px-4 py-2 bg-red-50 border-t border-red-200">
              <p className="text-sm text-red-600">{error}</p>
            </div>
          )}
          <form onSubmit={handleSubmit} className={`p-3 border-t mt-auto ${isMinimized ? 'hidden' : 'block'}`}>
            <div className="flex">
              <Textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  // Submit on Ctrl+Enter or Cmd+Enter
                  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                    e.preventDefault();
                    handleSubmit(e);
                  }
                }}
                placeholder="Ask a question... (Press Ctrl+Enter to send)"
                className="resize-none min-h-[60px]"
              />
              <Button 
                type="submit" 
                disabled={isLoading || isSubmitting} 
                className="ml-2"
              >
                {isLoading ? "Sending..." : "Send"}
                <Send size={16} />
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
};

export default AIAssistant;