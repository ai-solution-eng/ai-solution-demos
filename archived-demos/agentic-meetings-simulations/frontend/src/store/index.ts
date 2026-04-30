import { create } from 'zustand'

export interface EventLogItem {
  type: string
  agent_id?: string
  content?: string
  private_reasoning?: string
  name?: string
  timestamp?: string
  [key: string]: any
}

export interface MeetingState {
  activeMeetingId: string | null;
  status: 'draft' | 'running' | 'completed' | 'terminated' | 'queued' | 'failed' | null;
  currentTurn: number;
  eventLog: EventLogItem[];
  attendees: Record<string, any>;
  objective: string;
  typingAgentId: string | null;
  
  setActiveMeetingId: (id: string | null) => void;
  setStatus: (status: any) => void;
  setMeetingData: (data: Partial<MeetingState>) => void;
  addEvent: (event: EventLogItem) => void;
  setTypingAgentId: (id: string | null) => void;
  resetMeeting: () => void;
}

export const useMeetingStore = create<MeetingState>((set) => ({
  activeMeetingId: null,
  status: null,
  currentTurn: 0,
  eventLog: [],
  attendees: {},
  objective: "",
  typingAgentId: null,
  
  setActiveMeetingId: (id: string | null) => set({ activeMeetingId: id }),
  setStatus: (status) => set({ status }),
  setMeetingData: (data) => set((state) => ({ ...state, ...data })),
  
  addEvent: (event) => set((state) => {
    const isThinking = event.type === 'agent_thinking' || event.type === 'supervisor_selected_next_agent';
    return { 
      eventLog: [...state.eventLog, event],
      currentTurn: event.type === 'supervisor_selected_next_agent' ? state.currentTurn + 1 : state.currentTurn,
      typingAgentId: isThinking ? event.agent_id || null : (event.type === 'agent_spoke' || event.type === 'supervisor_spoke' ? null : state.typingAgentId)
    }
  }),
  
  setTypingAgentId: (id) => set({ typingAgentId: id }),
  
  resetMeeting: () => set({
    activeMeetingId: null,
    status: null,
    currentTurn: 0,
    eventLog: [],
    attendees: {},
    objective: "",
    typingAgentId: null
  })
}))
