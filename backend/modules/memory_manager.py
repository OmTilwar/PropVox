from typing import Dict, List

class MemoryManager:
    def __init__(self):
        # Maps session_id to a list of message dicts: {"role": "...", "content": "..."}
        self.sessions: Dict[str, List[Dict[str, str]]] = {}
        # Maps session_id to the string summary of the conversation
        self.summaries: Dict[str, str] = {}

    def init_session(self, session_id: str, system_prompt: str) -> None:
        if session_id not in self.sessions:
            self.sessions[session_id] = [
                {"role": "system", "content": system_prompt}
            ]
            self.summaries[session_id] = ""

    def add_message(self, session_id: str, role: str, content: str) -> None:
        if session_id in self.sessions:
            self.sessions[session_id].append({"role": role, "content": content})
            # Safely bound to 100 so memory doesn't leak. get_history slices the context anyway.
            if len(self.sessions[session_id]) > 100: 
                self.sessions[session_id] = [self.sessions[session_id][0]] + self.sessions[session_id][-99:]

    def get_history(self, session_id: str, max_recent=8) -> List[Dict[str, str]]:
        if session_id not in self.sessions:
            return []
            
        system_msg = self.sessions[session_id][0]
        recent_msgs = self.sessions[session_id][1:]
        
        history = [system_msg]
        
        # Inject the live summary as a system instruction
        summary = self.summaries.get(session_id, "")
        if summary:
            history.append({
                "role": "system", 
                "content": f"[SYSTEM MEMORY - DO NOT REPEAT TO USER]\nPrevious Conversation Summary:\n{summary}\n[END SYSTEM MEMORY]"
            })
            
        # Append the last N messages (8 messages = 4 interactions)
        history.extend(recent_msgs[-max_recent:])
        return history

    def clear_session(self, session_id: str) -> None:
        if session_id in self.sessions:
            del self.sessions[session_id]
        if session_id in self.summaries:
            del self.summaries[session_id]

    def get_last_message(self, session_id: str):
        if session_id in self.sessions and len(self.sessions[session_id]) > 1:
            return self.sessions[session_id][-1]
        return None
        
    def pop_last_message(self, session_id: str, role: str = None) -> bool:
        if session_id in self.sessions and len(self.sessions[session_id]) > 1:
            if role is None or self.sessions[session_id][-1]["role"] == role:
                self.sessions[session_id].pop()
                return True
        return False

# Singleton instance
memory = MemoryManager()
