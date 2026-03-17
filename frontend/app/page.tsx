import VoiceAgent from "@/components/VoiceAgent";

export default function Home() {
  return (
    <main className="h-screen w-full relative overflow-hidden text-[#1A1A1A] font-sans selection:bg-[#1F6F5D] selection:text-white flex flex-col">
      
      {/* Decorative background blurs */}
      <div className="absolute top-[-10%] left-[-10%] w-[50%] h-[50%] bg-[#1F6F5D]/10 blur-[120px] rounded-full pointer-events-none" />
      <div className="absolute bottom-[-10%] right-[-10%] w-[50%] h-[50%] bg-[#0F3D34]/10 blur-[120px] rounded-full pointer-events-none" />

      <div className="relative z-10 flex flex-col items-center pt-8 sm:pt-12 px-4 sm:px-8 lg:px-16 w-full max-w-7xl mx-auto h-full pb-6">
        
        {/* Header section (Compact) */}
        <div className="text-center space-y-2 mb-6 animate-in fade-in slide-in-from-top-4 duration-1000 shrink-0">
          <div className="inline-block px-3 py-1 rounded-full border border-[#1F6F5D]/20 bg-[#1F6F5D]/5 text-[#1F6F5D] text-[10px] font-semibold tracking-widest uppercase mb-2">
            A Premium Township by Riverwood
          </div>
          <h1 className="text-3xl md:text-5xl font-light tracking-tight text-[#0B2F28]">
            Riverwood <span className="font-semibold text-[#0F3D34]">Estate</span>
          </h1>
          <p className="text-sm md:text-base text-gray-600 font-light max-w-2xl mx-auto leading-relaxed hidden sm:block">
            Experience smart, connected living at Sector 7, Kharkhauda.
          </p>
        </div>

        {/* Main Agent Interface - Fills remaining space */}
        <div className="w-full flex-1 shadow-2xl shadow-[#0F3D34]/10 rounded-3xl overflow-hidden ring-1 ring-black/5 animate-in fade-in zoom-in-95 duration-1000 delay-200 fill-mode-both flex flex-col bg-white min-h-0">
           <VoiceAgent />
        </div>
        
        <div className="mt-4 text-center text-xs text-gray-400 shrink-0">
           &copy; {new Date().getFullYear()} Riverwood Projects LLP. SEC-7 IMT Kharkhauda.
        </div>

      </div>
    </main>
  );
}
