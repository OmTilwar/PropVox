"use client";

import { useState, useEffect, useRef } from "react";
import { Mic, Square, Loader2, Send, Globe } from "lucide-react";

interface Message {
  role: "user" | "assistant";
  content: string;
  latency?: number;
}

export default function VoiceAgent() {
  const [selectedLanguage, setSelectedLanguage] = useState<"en" | "hi" | null>(null);
  const [isRecording, setIsRecording] = useState(false);
  const [isConnected, setIsConnected] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [isProcessing, setIsProcessing] = useState(false);
  const [textInput, setTextInput] = useState("");

  const wsRef = useRef<WebSocket | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const audioContextRef = useRef<AudioContext | null>(null);
  const audioQueueRef = useRef<AudioBuffer[]>([]);
  const isPlayingRef = useRef<boolean>(false);
  const pendingChunksRef = useRef<number>(0);
  const stopRequestedRef = useRef<boolean>(false);
  const decodeChainRef = useRef<Promise<void>>(Promise.resolve());
  const activeSourceRef = useRef<AudioBufferSourceNode | null>(null);
  const playbackSessionIdRef = useRef<number>(0);
  const isInterruptedRef = useRef<boolean>(false);   // Hard stop flag — stops ALL decode + playback instantly
  const isRecordingRef = useRef<boolean>(false);      // Mirrors isRecording state for use inside WS closure

  const playNextInQueue = (currentSessionId: number) => {
    // Stop if interrupted OR session changed
    if (
      isInterruptedRef.current ||
      audioQueueRef.current.length === 0 ||
      playbackSessionIdRef.current !== currentSessionId
    ) {
      isPlayingRef.current = false;
      return;
    }
    isPlayingRef.current = true;
    const buffer = audioQueueRef.current.shift()!;
    if (!audioContextRef.current || audioContextRef.current.state === "closed") return;
    
    const audioCtx = audioContextRef.current;
    const source = audioCtx.createBufferSource();
    activeSourceRef.current = source;
    source.buffer = buffer;
    source.connect(audioCtx.destination);
    source.onended = () => {
      if (activeSourceRef.current === source) activeSourceRef.current = null;
      playNextInQueue(currentSessionId);
    };
    source.start(0);
  };

  useEffect(() => {
    if (!selectedLanguage) return;

    // Initialize WebSocket with language query param
    const backendUrl = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000";
    const ws = new WebSocket(`${backendUrl}/ws/voice?language=${selectedLanguage}`);
    
    
    
    ws.onopen = () => {
      setIsConnected(true);
      console.log("Connected to server");
    };

    ws.binaryType = "arraybuffer";
    ws.onmessage = async (event) => {
      // 1. Raw Binary Audio Frame (Fastest Path)
      if (event.data instanceof ArrayBuffer) {
        playAudioBinary(event.data);
        return;
      }

      // 2. JSON Transcripts
      if (typeof event.data === "string") {
        const payload = JSON.parse(event.data);
        if (payload.type === "transcript") {
            const isUserLive    = payload.role === "user" && payload.subtype === "live";
            const isUserInterim = payload.role === "user" && payload.subtype === "interim";
            const isUserFinal   = payload.role === "user" && payload.subtype === "final";

            setMessages((prev) => {
              const lastMsg = prev[prev.length - 1];
              // live / interim / final: all update the current user bubble in-place
              if ((isUserLive || isUserInterim || isUserFinal) && lastMsg?.role === "user") {
                return [
                  ...prev.slice(0, -1),
                  { role: payload.role, content: payload.text, latency: payload.latency }
                ];
              }
              // No existing user bubble yet — create one
              if (isUserLive || isUserInterim || isUserFinal) {
                return [...prev, { role: payload.role, content: payload.text, latency: payload.latency }];
              }
              // Assistant message — always append
              return [...prev, { role: payload.role, content: payload.text, latency: payload.latency }];
            });

            if (payload.role === "assistant") {
              setIsProcessing(false);
            }

        } else if (payload.type === "interrupt") {
          console.log("🛑 Frontend received interrupt signal.");

          // ── STALE INTERRUPT GUARD ──────────────────────────────────────────────────
          // If we're already recording, this interrupt came from the backend's
          // barge-in detector seeing the OLD processing task before our own
          // interrupt message was processed. It's stale — ignore it so it
          // doesn't re-set isInterruptedRef after we already cleared it.
          if (isRecordingRef.current) {
            console.log("⚠️  Ignoring stale backend interrupt (already recording).");
            setIsProcessing(false);
            return;
          }

          // 1. Set hard-stop flag FIRST — all pending decode callbacks check this
          isInterruptedRef.current = true;

          // 2. Bump session ID to invalidate any queued/decoded audio frames
          playbackSessionIdRef.current += 1;

          // 3. Stop the actively playing audio source immediately
          if (activeSourceRef.current) {
            try { activeSourceRef.current.stop(); } catch (e) {}
            activeSourceRef.current = null;
          }

          // 4. Close & discard the AudioContext so no buffered audio can play
          if (audioContextRef.current && audioContextRef.current.state !== "closed") {
            audioContextRef.current.close().catch(console.error);
          }
          audioContextRef.current = null;

          // 5. Clear all state
          audioQueueRef.current = [];
          isPlayingRef.current = false;
          decodeChainRef.current = Promise.resolve();
          setIsProcessing(false);
        }
      }
    };

    ws.onclose = () => setIsConnected(false);
    wsRef.current = ws;

    return () => {
      ws.close();
      if (audioContextRef.current && audioContextRef.current.state !== "closed") {
        audioContextRef.current.close().catch(console.error);
      }
      audioContextRef.current = null;
      audioQueueRef.current = [];
      isPlayingRef.current = false;
    };
  }, [selectedLanguage]);

  // We wrap the decode + queue logic in a strict Promise chain so that
  // smaller chunks that decode quickly don't leapfrog larger chunks that take longer.
  const playAudioBinary = (audioData: ArrayBuffer) => {
    const capturedSessionId = playbackSessionIdRef.current;
    decodeChainRef.current = decodeChainRef.current.then(async () => {
      // ── GUARD 1: bail out immediately if interrupted OR session changed ──
      if (isInterruptedRef.current || playbackSessionIdRef.current !== capturedSessionId) return;

      try {
        // ── GUARD 2: don't create a new AudioContext while interrupted ──
        if (isInterruptedRef.current || playbackSessionIdRef.current !== capturedSessionId) return;
        
        if (!audioContextRef.current || audioContextRef.current.state === "closed") {
          audioContextRef.current = new (window.AudioContext || (window as any).webkitAudioContext)();
        }
        
        const audioCtx = audioContextRef.current;

        // ── GUARD 3: check BEFORE starting the heavy async decode ──
        if (isInterruptedRef.current || playbackSessionIdRef.current !== capturedSessionId) return;

        const audioBuffer = await audioCtx.decodeAudioData(audioData.slice(0));
        
        // ── GUARD 4: check AFTER decode completes (can take 10-200ms) ──
        if (isInterruptedRef.current || playbackSessionIdRef.current !== capturedSessionId) return;

        audioQueueRef.current.push(audioBuffer);
        if (!isPlayingRef.current) {
          playNextInQueue(capturedSessionId);
        }
      } catch (error) {
        // Swallow AbortError — this fires when AudioContext is closed mid-decode (expected on interrupt)
        if ((error as DOMException)?.name !== "AbortError") {
          console.error("Audio playback failed:", error);
        }
      }
    });
  };

  const startRecording = async () => {
    try {
      // ── STEP 1: Set the hard-stop interrupt flag BEFORE anything else ──
      isInterruptedRef.current = true;

      // ── STEP 2: Stop actively playing audio ──
      if (activeSourceRef.current) {
        try { activeSourceRef.current.stop(); } catch (e) {}
        activeSourceRef.current = null;
      }

      // ── STEP 3: Bump session ID ──
      playbackSessionIdRef.current += 1;

      // ── STEP 4: Close AudioContext ──
      if (audioContextRef.current && audioContextRef.current.state !== "closed") {
        audioContextRef.current.close().catch(console.error);
      }
      audioContextRef.current = null;

      // ── STEP 5: Clear all audio state ──
      audioQueueRef.current = [];
      isPlayingRef.current = false;
      pendingChunksRef.current = 0;
      stopRequestedRef.current = false;
      decodeChainRef.current = Promise.resolve();

      // ── STEP 6: Notify backend BEFORE getUserMedia ─────────────────────────────
      // Sending interrupt HERE (not after getUserMedia) cuts the backend cancel
      // latency from 100–500ms down to ~0ms. The backend stops sending old audio
      // immediately. isRecordingRef is set so any stale interrupt echo from
      // Deepgram's barge-in detector is ignored in the onmessage handler.
      isRecordingRef.current = true;
      setIsRecording(true);
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "interrupt" }));
        wsRef.current.send(JSON.stringify({ type: "audio_start" }));
        setIsProcessing(false);
      }

      // ── STEP 7: Get mic access (may take 100–500ms) ──
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          ...({
            echoCancellationType: "system",
            suppressLocalAudioPlayback: true,
            channelCount: 1,
            sampleRate: 16000,
            latency: 0,
          } as any)
        }
      });
      const mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });

      audioChunksRef.current = [];

      // ── STEP 8: Clear interrupt flag — new audio from next response can now play ──
      isInterruptedRef.current = false;

      mediaRecorder.ondataavailable = async (event) => {
        if (event.data.size > 0) {
          audioChunksRef.current.push(event.data);
          
          pendingChunksRef.current += 1;

          // Use arrayBuffer for synchronous-style encoding - no FileReader race condition
          const arrayBuf = await event.data.arrayBuffer();
          const bytes = new Uint8Array(arrayBuf);
          let binary = '';
          for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
          const base64Audio = window.btoa(binary);

          if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ type: "audio_chunk", data: base64Audio }));
          }

          pendingChunksRef.current -= 1;

          // If stop was requested and this was the last in-flight chunk, now send audio_stop
          if (stopRequestedRef.current && pendingChunksRef.current === 0) {
            stopRequestedRef.current = false;
            if (wsRef.current?.readyState === WebSocket.OPEN) {
              setIsProcessing(true);
              wsRef.current.send(JSON.stringify({ type: "audio_stop" }));
            }
          }
        }
      };

      mediaRecorder.onstop = async () => {
        // If there are still chunks being encoded, flag stop so the last chunk sends it
        if (pendingChunksRef.current > 0) {
          stopRequestedRef.current = true;
        } else {
          // No pending chunks — safe to signal stop immediately
          if (wsRef.current?.readyState === WebSocket.OPEN) {
            setIsProcessing(true);
            wsRef.current.send(JSON.stringify({ type: "audio_stop" }));
          }
        }
        
        // Stop all tracks to release microphone
        stream.getTracks().forEach(track => track.stop());
      };

      mediaRecorderRef.current = mediaRecorder;
      mediaRecorder.start(250);
    } catch (error) {
      isRecordingRef.current = false;
      isInterruptedRef.current = false;
      setIsRecording(false);
      console.error("Error accessing microphone:", error);
      alert("Could not access microphone.");
    }
  };

  const stopRecording = () => {
    if (mediaRecorderRef.current && isRecording) {
      mediaRecorderRef.current.stop();
      isRecordingRef.current = false;
      setIsRecording(false);
    }
  };

  const handleSendText = () => {
    if (!textInput.trim() || !wsRef.current) return;
    
    // Stop any currently playing audio immediately
    isInterruptedRef.current = true;
    
    if (activeSourceRef.current) {
      try { activeSourceRef.current.stop(); } catch (e) {}
      activeSourceRef.current = null;
    }
    
    playbackSessionIdRef.current += 1;
    
    if (audioContextRef.current && audioContextRef.current.state !== "closed") {
      audioContextRef.current.close().catch(console.error);
    }
    audioContextRef.current = null;
    audioQueueRef.current = [];
    isPlayingRef.current = false;
    decodeChainRef.current = Promise.resolve();

    // Clear interrupt flag so new response can play
    isInterruptedRef.current = false;

    setMessages(prev => [...prev, { role: "user", content: textInput }]);
    setIsProcessing(true);
    wsRef.current.send(JSON.stringify({ type: "interrupt" }));
    wsRef.current.send(JSON.stringify({ type: "text", text: textInput }));
    setTextInput("");
  };

  if (!selectedLanguage) {
    return (
      <div className="flex flex-col h-full w-full max-w-2xl mx-auto bg-white rounded-2xl shadow-xl border border-gray-100 overflow-hidden p-8 items-center justify-center space-y-8">
        <Globe className="w-16 h-16 text-[#0F3D34] opacity-80" />
        <div className="text-center space-y-2">
          <h2 className="text-2xl font-bold text-[#1A1A1A]">Select Language</h2>
          <p className="text-sm text-gray-500">Choose your preferred language for the conversation</p>
        </div>
        <div className="flex flex-row space-x-4 w-full max-w-sm">
          <button 
            onClick={() => setSelectedLanguage("en")}
            className="flex-1 flex flex-col items-center justify-center p-6 bg-gray-50 hover:bg-[#F4F7F6] border-2 border-transparent hover:border-[#1F6F5D] rounded-xl transition-all"
          >
            <span className="text-3xl mb-2">🇬🇧</span>
            <span className="font-semibold text-[#1A1A1A]">English</span>
          </button>
          
          <button 
            onClick={() => setSelectedLanguage("hi")}
            className="flex-1 flex flex-col items-center justify-center p-6 bg-gray-50 hover:bg-[#F4F7F6] border-2 border-transparent hover:border-[#1F6F5D] rounded-xl transition-all"
          >
            <span className="text-3xl mb-2">🇮🇳</span>
            <span className="font-semibold text-[#1A1A1A]">Hindi</span>
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full w-full max-w-2xl mx-auto bg-white rounded-2xl shadow-xl border border-gray-100 overflow-hidden">
      
      {/* Header */}
      <div className="bg-[#0F3D34] text-white p-6 flex flex-col items-center justify-center relative">
        <div className="absolute top-4 left-4">
          <button 
             onClick={() => {
                setSelectedLanguage(null);
                setMessages([]);
                if (wsRef.current) wsRef.current.close();
                if (audioContextRef.current && audioContextRef.current.state !== "closed") {
                   audioContextRef.current.close().catch(console.error);
                }
                audioContextRef.current = null;
                audioQueueRef.current = [];
                isPlayingRef.current = false;
             }}
             className="text-xs text-[#F4F7F6] opacity-70 hover:opacity-100 transition-opacity flex items-center bg-black/20 px-2 py-1 rounded"
          >
             Change Lang
          </button>
        </div>
        <div className="absolute top-4 right-4 flex items-center space-x-2">
          <div className={`w-2 h-2 rounded-full ${isConnected ? 'bg-green-400' : 'bg-red-500'}`}></div>
          <span className="text-xs opacity-80">{isConnected ? 'Connected' : 'Offline'}</span>
        </div>
        <h2 className="text-2xl font-bold tracking-tight">Myra</h2>
        <p className="text-[#F4F7F6] text-sm mt-1 opacity-90">
           Riverwood Estate Voice Assistant ({selectedLanguage === 'hi' ? 'Hindi' : 'English'})
        </p>
      </div>

      {/* Chat History */}
      <div className="flex-1 p-6 overflow-y-auto bg-gray-50 flex flex-col space-y-4">
        {messages.length === 0 ? (
          <div className="flex-1 flex flex-col items-center justify-center text-gray-400 space-y-3">
            <Mic className="w-12 h-12 opacity-20" />
            <p>Tap the microphone to ask about Sector 7...</p>
          </div>
        ) : (
          messages.map((msg, idx) => (
            <div key={idx} className={`flex flex-col ${msg.role === "user" ? "items-end" : "items-start"}`}>
              <div className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                <div className={`max-w-[80%] p-4 rounded-2xl text-sm ${
                  msg.role === "user" 
                    ? "bg-[#1F6F5D] text-white rounded-br-none" 
                    : "bg-white border border-gray-200 text-[#1A1A1A] rounded-bl-none shadow-sm"
                }`}>
                  {msg.content}
                </div>
              </div>
              {msg.latency && (
                <div className="mt-1 text-xs text-[#1F6F5D] font-medium opacity-80 pl-2">
                  ⚡ Response Time: {msg.latency.toFixed(2)}s
                </div>
              )}
            </div>
          ))
        )}
        
        {isProcessing && (
          <div className="flex justify-start">
             <div className="bg-white border border-gray-200 p-4 rounded-2xl rounded-bl-none shadow-sm flex items-center space-x-2">
               <Loader2 className="w-4 h-4 text-[#1F6F5D] animate-spin" />
               <span className="text-sm text-gray-500">Myra is thinking...</span>
             </div>
          </div>
        )}
      </div>

      {/* Controls */}
      <div className="p-6 bg-white border-t border-gray-100 flex flex-col items-center">
        <button
          onClick={isRecording ? stopRecording : startRecording}
          disabled={!isConnected}
          className={`relative group flex items-center justify-center w-20 h-20 rounded-full transition-all duration-300 shadow-lg ${
            isRecording
              ? 'bg-red-500 hover:bg-red-600 animate-pulse'
              : isProcessing
                ? 'bg-amber-500 hover:bg-amber-600 hover:scale-105'  // Myra is speaking — click to interrupt
                : !isConnected
                  ? 'bg-gray-200 cursor-not-allowed'
                  : 'bg-[#1F6F5D] hover:bg-[#0F3D34] hover:scale-105'
          }`}
        >
          {isRecording ? (
            <Square className="w-8 h-8 text-white fill-current" />
          ) : isProcessing ? (
            <Mic className="w-8 h-8 text-white" />  // amber = tap to interrupt
          ) : (
            <Mic className={`w-8 h-8 ${!isConnected ? 'text-gray-400' : 'text-white'}`} />
          )}
          {/* Ripple effect when recording */}
          {isRecording && (
            <div className="absolute inset-0 rounded-full border-4 border-red-500 opacity-20 animate-ping"></div>
          )}
          {/* Pulse ring while Myra is speaking */}
          {isProcessing && !isRecording && (
            <div className="absolute inset-0 rounded-full border-4 border-amber-400 opacity-30 animate-ping"></div>
          )}
        </button>
        <p className="mt-4 text-sm font-medium text-slate-500">
          {isRecording
            ? "Call Active - Tap to Hang Up"
            : isProcessing
              ? "Myra is speaking — Tap to Interrupt"
              : "Tap to Start Call"}
        </p>

        {/* Fallback Text Input */}
        <div className="w-full flex items-center space-x-2 relative mt-4">
          <input
            type="text"
            value={textInput}
            onChange={(e) => setTextInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSendText()}
            placeholder="Or type a message..."
            disabled={!isConnected}
            className="flex-1 bg-gray-50 border border-gray-200 rounded-full px-5 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-[#1F6F5D] focus:border-transparent transition-all disabled:opacity-50"
          />
          <button
            onClick={handleSendText}
            disabled={!textInput.trim() || !isConnected}
            className="absolute right-2 p-2 bg-[#1F6F5D] text-white rounded-full hover:bg-[#0F3D34] disabled:opacity-50 disabled:hover:bg-[#1F6F5D] transition-colors"
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
